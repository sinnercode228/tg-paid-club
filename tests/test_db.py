from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from paidclub.billing.base import PaymentPurpose
from paidclub.db import (
    SessionFactory,
    claim,
    create_all,
    create_engine,
    in_transaction,
    insert_ignore,
    make_sessionmaker,
    release,
)
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import (
    Member,
    Notification,
    Payment,
    PaymentOutcome,
    PaymentStatus,
    Subscription,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LEASE = timedelta(minutes=5)


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[SessionFactory]:
    engine = create_engine(database_url)
    await create_all(engine)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def add_member(session: AsyncSession, telegram_id: int) -> Member:
    member = Member(telegram_id=telegram_id, created_at=NOW)
    session.add(member)
    await session.flush()
    return member


def new_sub(member_id: int, status: SubStatus = SubStatus.ACTIVE) -> Subscription:
    return Subscription(
        member_id=member_id,
        plan_code="month",
        provider="fake",
        status=status,
        period_unit="month",
        period_count=1,
        price_minor=99000,
        currency="RUB",
        next_charge_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def new_payment(sub_id: int, provider_id: str, *, status: PaymentStatus, applied: bool) -> Payment:
    return Payment(
        provider="fake",
        provider_payment_id=provider_id,
        subscription_id=sub_id,
        purpose=PaymentPurpose.RENEWAL,
        status=status,
        outcome=PaymentOutcome.APPLIED if applied else PaymentOutcome.RECORDED,
        applied=applied,
        amount_minor=99000,
        currency="RUB",
        for_period=2,
        occurred_at=NOW,
        created_at=NOW,
    )


async def test_datetimes_are_stored_and_returned_as_aware_utc(sessions: SessionFactory) -> None:
    moscow = datetime(2026, 3, 1, 15, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    async with sessions() as session, session.begin():
        member = Member(telegram_id=1, created_at=moscow)
        session.add(member)
    async with sessions() as session:
        loaded = (await session.scalars(select(Member))).one()
        assert loaded.created_at.tzinfo is UTC
        assert loaded.created_at == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        # Comparisons in SQL use the same UTC representation.
        found = await session.scalar(select(Member.id).where(Member.created_at < NOW + LEASE))
        assert found == loaded.id


async def test_naive_datetimes_are_rejected(sessions: SessionFactory) -> None:
    async with sessions() as session:
        session.add(Member(telegram_id=1, created_at=datetime(2026, 3, 1, 12, 0)))
        with pytest.raises(StatementError, match="naive datetime"):
            await session.flush()


async def test_two_concurrent_claims_never_get_the_same_row(sessions: SessionFactory) -> None:
    async with sessions() as session, session.begin():
        for telegram_id in range(20):
            member = await add_member(session, telegram_id)
            session.add(new_sub(member.id))

    async def grab() -> list[int]:
        async with sessions() as session, session.begin():
            _, ids = await claim(
                session,
                Subscription,
                Subscription.status == SubStatus.ACTIVE,
                now=NOW,
                lease=LEASE,
                limit=15,
                order_by=Subscription.id,
            )
            await asyncio.sleep(0)
            return ids

    first, second = await asyncio.gather(grab(), grab())
    assert not set(first) & set(second)
    # On SQLite the loser may come back empty-handed (no SKIP LOCKED); nothing is lost, the
    # rows it missed are simply free for the next claim.
    rest = await grab()
    assert sorted(first + second + rest) == list(range(1, 21))


async def test_a_claim_is_free_again_once_its_lease_runs_out(sessions: SessionFactory) -> None:
    async with sessions() as session, session.begin():
        member = await add_member(session, 1)
        session.add(new_sub(member.id))

    async def grab(at: datetime) -> list[int]:
        async with sessions() as session, session.begin():
            _, ids = await claim(session, Subscription, now=at, lease=LEASE, limit=10)
            return ids

    assert len(await grab(NOW)) == 1
    assert await grab(NOW + LEASE - timedelta(seconds=1)) == []
    assert len(await grab(NOW + LEASE)) == 1


async def test_release_can_keep_a_row_untouchable_for_a_while(sessions: SessionFactory) -> None:
    async with sessions() as session, session.begin():
        member = await add_member(session, 1)
        sub = new_sub(member.id)
        session.add(sub)
    async with sessions() as session, session.begin():
        token, [sub_id] = await claim(session, Subscription, now=NOW, lease=LEASE, limit=1)
        assert await release(session, Subscription, sub_id, "someone-else") is False
        assert await release(session, Subscription, sub_id, token, until=NOW + timedelta(hours=1))
    async with sessions() as session, session.begin():
        _, ids = await claim(session, Subscription, now=NOW + LEASE, lease=LEASE, limit=1)
        assert ids == []
        _, ids = await claim(
            session, Subscription, now=NOW + timedelta(hours=1), lease=LEASE, limit=1
        )
        assert ids == [sub_id]


async def test_a_member_has_at_most_one_live_subscription(sessions: SessionFactory) -> None:
    async with sessions() as session, session.begin():
        member = await add_member(session, 1)
        session.add_all(
            [
                new_sub(member.id, SubStatus.EXPIRED),
                new_sub(member.id, SubStatus.EXPIRED),
                new_sub(member.id, SubStatus.PENDING),
                new_sub(member.id, SubStatus.ACTIVE),
            ]
        )
        member_id = member.id
    for status in (SubStatus.ACTIVE, SubStatus.PAST_DUE, SubStatus.CANCELED):
        async with sessions() as session:
            session.add(new_sub(member_id, status))
            with pytest.raises(IntegrityError):
                await session.flush()


async def test_the_optimistic_lock_rejects_the_second_writer(sessions: SessionFactory) -> None:
    async with sessions() as session, session.begin():
        member = await add_member(session, 1)
        sub = new_sub(member.id)
        session.add(sub)
        await session.flush()
        sub_id = sub.id

    async with sessions() as first, sessions() as second:
        a = await first.get(Subscription, sub_id)
        b = await second.get(Subscription, sub_id)
        assert a is not None and b is not None
        a.periods_paid = 2
        await first.commit()
        b.periods_paid = 2
        with pytest.raises(StaleDataError):
            await second.commit()


async def test_in_transaction_retries_on_a_fresh_session(sessions: SessionFactory) -> None:
    calls: list[int] = []

    async def work(session: AsyncSession) -> str:
        calls.append(id(session))
        if len(calls) == 1:
            raise StaleDataError("lost the race")
        return "done"

    assert await in_transaction(sessions, work) == "done"
    assert len(calls) == 2
    assert calls[0] != calls[1]


async def test_a_period_is_paid_by_at_most_one_applied_payment(sessions: SessionFactory) -> None:
    async with sessions() as session, session.begin():
        member = await add_member(session, 1)
        sub = new_sub(member.id)
        session.add(sub)
        await session.flush()
        sub_id = sub.id
        session.add_all(
            [
                new_payment(sub_id, "p1", status=PaymentStatus.SUCCEEDED, applied=True),
                new_payment(sub_id, "p2", status=PaymentStatus.SUCCEEDED, applied=False),
                new_payment(sub_id, "p3", status=PaymentStatus.FAILED, applied=True),
            ]
        )
    async with sessions() as session:
        session.add(new_payment(sub_id, "p4", status=PaymentStatus.SUCCEEDED, applied=True))
        with pytest.raises(IntegrityError):
            await session.flush()
    async with sessions() as session:
        session.add(new_payment(sub_id, "p1", status=PaymentStatus.FAILED, applied=False))
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_insert_ignore_reports_whether_it_inserted(sessions: SessionFactory) -> None:
    values = {
        "key": "reminder:1:20260301T120000Z:3d",
        "kind": "reminder",
        "chat_id": 1,
        "text": "hi",
        "buttons": [],
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": NOW,
        "created_at": NOW,
    }
    table = Notification.__table__
    async with sessions() as session, session.begin():
        assert await insert_ignore(session, table, values, ["key"]) is True  # type: ignore[arg-type]
        assert await insert_ignore(session, table, {**values, "text": "changed"}, ["key"]) is False  # type: ignore[arg-type]
    async with sessions() as session:
        rows = (await session.scalars(select(Notification))).all()
        assert [row.text for row in rows] == ["hi"]
