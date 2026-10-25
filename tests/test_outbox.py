from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from paidclub.clock import FakeClock
from paidclub.db import SessionFactory, claim, create_all, create_engine, make_sessionmaker
from paidclub.models import Member, Notification, NotificationStatus
from paidclub.notifications import (
    Button,
    MessageDraft,
    OutboxSender,
    OutgoingMessage,
    RetryAfter,
    Undeliverable,
    enqueue,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.script: dict[int, list[Exception]] = {}

    async def __call__(self, message: OutgoingMessage) -> None:
        await asyncio.sleep(0)
        errors = self.script.get(message.chat_id)
        if errors:
            raise errors.pop(0)
        self.sent.append(message)


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[SessionFactory]:
    engine = create_engine(database_url)
    await create_all(engine)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def sender(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder, **kw: object
) -> OutboxSender:
    return OutboxSender(sessions, clock, recorder, lease=timedelta(minutes=5), **kw)  # type: ignore[arg-type]


async def put(
    sessions: SessionFactory,
    clock: FakeClock,
    key: str,
    *,
    chat_id: int = 1,
    member_id: int | None = None,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> bool:
    async with sessions() as session, session.begin():
        return await enqueue(
            session,
            key=key,
            kind="test",
            chat_id=chat_id,
            member_id=member_id,
            message=MessageDraft(f"text {key}", (Button("Оплатить", callback_data="pay:1"),)),
            now=clock.now(),
            not_before=not_before,
            not_after=not_after,
        )


async def rows(sessions: SessionFactory) -> list[Notification]:
    async with sessions() as session:
        return list((await session.scalars(select(Notification).order_by(Notification.id))).all())


async def test_the_same_key_is_enqueued_once(sessions: SessionFactory, clock: FakeClock) -> None:
    assert await put(sessions, clock, "reminder:1:20260304T120000Z:3d") is True
    assert await put(sessions, clock, "reminder:1:20260304T120000Z:3d") is False
    assert len(await rows(sessions)) == 1


async def test_a_message_is_sent_once_and_not_again_after_a_restart(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    await put(sessions, clock, "renewed:1:2", chat_id=42)
    report = await sender(sessions, clock, recorder).run_once()
    assert report.sent == 1
    [message] = recorder.sent
    assert message.chat_id == 42
    assert message.text == "text renewed:1:2"
    assert message.buttons == (Button("Оплатить", callback_data="pay:1"),)

    restarted = sender(sessions, clock, recorder)
    clock.advance(timedelta(days=1))
    assert (await restarted.run_once()).claimed == 0
    assert len(recorder.sent) == 1
    [row] = await rows(sessions)
    assert row.status is NotificationStatus.SENT
    assert row.sent_at == NOW


async def test_two_senders_running_together_send_each_message_once(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    for i in range(30):
        await put(sessions, clock, f"k{i}", chat_id=i)
    first = sender(sessions, clock, recorder, batch_size=7)
    second = sender(sessions, clock, recorder, batch_size=7)
    for _ in range(10):
        await asyncio.gather(first.run_once(), second.run_once())
    sent = [message.chat_id for message in recorder.sent]
    assert sorted(sent) == list(range(30))


async def test_flood_control_pushes_back_the_rest_of_the_batch(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    for chat in (1, 2, 3):
        await put(sessions, clock, f"k{chat}", chat_id=chat)
    recorder.script[2] = [RetryAfter(7)]
    outbox = sender(sessions, clock, recorder)

    report = await outbox.run_once()
    assert report.sent == 1
    assert report.retry_after == 7
    assert report.retried == 2
    assert (await outbox.run_once()).claimed == 0

    clock.advance(timedelta(seconds=7))
    assert (await outbox.run_once()).sent == 2
    assert [message.chat_id for message in recorder.sent] == [1, 2, 3]
    # Flood control is not the message's fault: it does not count as a failed attempt.
    assert all(row.attempts == 0 for row in await rows(sessions))


async def test_a_blocked_bot_makes_the_message_undeliverable(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    async with sessions() as session, session.begin():
        member = Member(telegram_id=5, created_at=NOW)
        session.add(member)
        await session.flush()
        member_id = member.id
    await put(sessions, clock, "k", chat_id=5, member_id=member_id)
    recorder.script[5] = [Undeliverable("Forbidden: bot was blocked by the user")]
    outbox = sender(sessions, clock, recorder)

    assert (await outbox.run_once()).undeliverable == 1
    clock.advance(timedelta(hours=2))
    assert (await outbox.run_once()).claimed == 0
    [row] = await rows(sessions)
    assert row.status is NotificationStatus.UNDELIVERABLE
    assert row.last_error == "Forbidden: bot was blocked by the user"
    async with sessions() as session:
        blocked = await session.get(Member, member_id)
        assert blocked is not None
        assert blocked.bot_blocked_at == NOW


async def test_transient_errors_back_off_and_then_give_up(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    await put(sessions, clock, "k", chat_id=9)
    recorder.script[9] = [ConnectionError("reset")] * 3
    outbox = sender(sessions, clock, recorder, max_attempts=3, base_backoff=timedelta(seconds=30))

    assert (await outbox.run_once()).retried == 1
    [row] = await rows(sessions)
    assert row.attempts == 1
    assert row.next_attempt_at == NOW + timedelta(seconds=30)

    clock.advance(timedelta(seconds=29))
    assert (await outbox.run_once()).claimed == 0
    clock.advance(timedelta(seconds=1))
    assert (await outbox.run_once()).retried == 1
    [row] = await rows(sessions)
    assert row.next_attempt_at == clock.now() + timedelta(seconds=60)

    clock.advance(timedelta(seconds=60))
    assert (await outbox.run_once()).failed == 1
    [row] = await rows(sessions)
    assert row.status is NotificationStatus.FAILED
    assert row.attempts == 3
    assert row.last_error == "ConnectionError: reset"
    assert recorder.sent == []


async def test_a_message_that_missed_its_deadline_is_dropped(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    await put(sessions, clock, "reminder", not_after=NOW + timedelta(hours=1))
    clock.advance(timedelta(hours=2))
    assert (await sender(sessions, clock, recorder).run_once()).stale == 1
    assert recorder.sent == []
    [row] = await rows(sessions)
    assert row.status is NotificationStatus.STALE


async def test_not_before_delays_a_message(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    await put(sessions, clock, "later", not_before=NOW + timedelta(minutes=10))
    outbox = sender(sessions, clock, recorder)
    assert (await outbox.run_once()).claimed == 0
    clock.advance(timedelta(minutes=10))
    assert (await outbox.run_once()).sent == 1


async def test_a_sender_that_died_after_claiming_is_replaced_after_its_lease(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    await put(sessions, clock, "k")
    async with sessions() as session, session.begin():
        _, ids = await claim(
            session, Notification, now=clock.now(), lease=timedelta(minutes=5), limit=10
        )
        assert len(ids) == 1
    # ...and the worker that claimed it crashed here.
    outbox = sender(sessions, clock, recorder)
    assert (await outbox.run_once()).claimed == 0
    clock.advance(timedelta(minutes=5))
    assert (await outbox.run_once()).sent == 1
    assert len(recorder.sent) == 1


async def test_the_loop_stops_when_asked(
    sessions: SessionFactory, clock: FakeClock, recorder: Recorder
) -> None:
    await put(sessions, clock, "k")
    stop = asyncio.Event()
    outbox = sender(sessions, clock, recorder)
    task = asyncio.create_task(outbox.run(stop, idle_interval=0.01))
    for _ in range(100):
        if recorder.sent:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert len(recorder.sent) == 1
