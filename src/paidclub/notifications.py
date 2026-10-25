"""Outbox for messages to members and the owner.

Code that changes a subscription enqueues the message in the same transaction
(:func:`enqueue`), so a message exists if and only if the change was committed. The ``key``
names the logical event (``reminder:<sub>:<period_end>:<offset>``, ``renewed:<sub>:<period>``)
and is unique, so a second worker, a retried webhook or a restart cannot enqueue it again.

:class:`OutboxSender` delivers rows through an injected ``send`` callable, which keeps it
independent of aiogram: the bot adapts Telegram's errors to :class:`RetryAfter` (429) and
:class:`Undeliverable` (403 / chat not found). Delivery is claimed with a lease, so two senders
never send the same row at the same time; a sender that crashes after Telegram accepted a
message but before recording it will resend that one message once its lease runs out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub.clock import Clock
from paidclub.db import SessionFactory, claim, insert_ignore
from paidclub.models import Member, Notification, NotificationStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Button:
    """An inline button: either a link or a callback the bot handles (see ``callbacks``)."""

    text: str
    url: str | None = None
    callback_data: str | None = None

    def __post_init__(self) -> None:
        if (self.url is None) == (self.callback_data is None):
            raise ValueError("a button needs exactly one of url or callback_data")
        if self.callback_data is not None and len(self.callback_data.encode()) > 64:
            raise ValueError("Telegram limits callback_data to 64 bytes")

    def to_dict(self) -> dict[str, str]:
        data = {"text": self.text}
        if self.url is not None:
            data["url"] = self.url
        if self.callback_data is not None:
            data["callback_data"] = self.callback_data
        return data

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Button:
        return cls(text=data["text"], url=data.get("url"), callback_data=data.get("callback_data"))


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """Plain text (no parse mode) plus buttons, one per row."""

    text: str
    buttons: tuple[Button, ...] = ()


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    notification_id: int
    kind: str
    chat_id: int
    text: str
    buttons: tuple[Button, ...]


class RetryAfter(Exception):
    """Telegram flood control (HTTP 429): wait ``seconds`` before sending anything else."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


class Undeliverable(Exception):
    """Telegram will never accept this message (bot blocked, user deactivated, chat gone)."""


SendFn = Callable[[OutgoingMessage], Awaitable[None]]


async def enqueue(
    session: AsyncSession,
    *,
    key: str,
    kind: str,
    chat_id: int,
    message: MessageDraft,
    now: datetime,
    member_id: int | None = None,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> bool:
    """Add a message to the outbox in the caller's transaction.

    Returns ``False`` when a message with this ``key`` already exists (it is never replaced).
    ``not_after`` drops a message that could not go out in time, such as a reminder about a
    date that has already passed.
    """
    return await insert_ignore(
        session,
        Notification.__table__,  # type: ignore[arg-type]
        {
            "key": key,
            "kind": kind,
            "chat_id": chat_id,
            "member_id": member_id,
            "text": message.text,
            "buttons": [button.to_dict() for button in message.buttons],
            "status": NotificationStatus.PENDING.value,
            "attempts": 0,
            "next_attempt_at": not_before or now,
            "not_after": not_after,
            "created_at": now,
        },
        ["key"],
    )


@dataclass
class SendReport:
    sent: int = 0
    retried: int = 0
    undeliverable: int = 0
    failed: int = 0
    stale: int = 0
    #: Set when Telegram asked us to slow down; the loop sleeps this long.
    retry_after: float | None = None
    claimed: int = 0
    errors: list[str] = field(default_factory=list)


class OutboxSender:
    def __init__(
        self,
        sessions: SessionFactory,
        clock: Clock,
        send: SendFn,
        *,
        batch_size: int = 20,
        lease: timedelta = timedelta(minutes=5),
        max_attempts: int = 8,
        base_backoff: timedelta = timedelta(seconds=30),
        max_backoff: timedelta = timedelta(hours=1),
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._send = send
        self._batch_size = batch_size
        self._lease = lease
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff

    def _backoff(self, attempts: int) -> timedelta:
        delay = self._base_backoff * (1 << max(attempts - 1, 0))
        return min(delay, self._max_backoff)

    async def run_once(self) -> SendReport:
        report = SendReport()
        now = self._clock.now()
        async with self._sessions() as session, session.begin():
            token, ids = await claim(
                session,
                Notification,
                Notification.status == NotificationStatus.PENDING,
                Notification.next_attempt_at <= now,
                now=now,
                lease=self._lease,
                limit=self._batch_size,
                order_by=Notification.id,
            )
            rows: Sequence[Notification] = []
            if ids:
                result = await session.scalars(
                    select(Notification).where(Notification.id.in_(ids)).order_by(Notification.id)
                )
                rows = result.all()
        report.claimed = len(rows)

        for index, row in enumerate(rows):
            now = self._clock.now()
            if row.not_after is not None and now > row.not_after:
                await self._finish(row.id, token, status=NotificationStatus.STALE)
                report.stale += 1
                continue
            message = OutgoingMessage(
                notification_id=row.id,
                kind=row.kind,
                chat_id=row.chat_id,
                text=row.text,
                buttons=tuple(Button.from_dict(item) for item in row.buttons or []),
            )
            try:
                await self._send(message)
            except RetryAfter as exc:
                # Flood control applies to the bot, not this one chat: push back the rest too.
                resume_at = now + timedelta(seconds=exc.seconds)
                for pending in rows[index:]:
                    await self._reschedule(pending.id, token, resume_at, count_attempt=False)
                report.retry_after = exc.seconds
                report.retried += len(rows) - index
                log.warning("telegram flood control: pausing outbox for %ss", exc.seconds)
                break
            except Undeliverable as exc:
                await self._finish(
                    row.id,
                    token,
                    status=NotificationStatus.UNDELIVERABLE,
                    error=str(exc) or type(exc).__name__,
                    blocked_member_id=row.member_id,
                )
                report.undeliverable += 1
            except Exception as exc:  # any transport error is retried with backoff
                attempts = row.attempts + 1
                error = f"{type(exc).__name__}: {exc}"
                report.errors.append(error)
                if attempts >= self._max_attempts:
                    await self._finish(
                        row.id,
                        token,
                        status=NotificationStatus.FAILED,
                        error=error,
                        attempts=attempts,
                    )
                    report.failed += 1
                else:
                    await self._reschedule(
                        row.id,
                        token,
                        now + self._backoff(attempts),
                        count_attempt=True,
                        error=error,
                    )
                    report.retried += 1
                log.warning("notification %s not sent (attempt %s): %s", row.id, attempts, error)
            else:
                await self._finish(row.id, token, status=NotificationStatus.SENT, sent_at=now)
                report.sent += 1
        return report

    async def _finish(
        self,
        notification_id: int,
        token: str,
        *,
        status: NotificationStatus,
        error: str | None = None,
        sent_at: datetime | None = None,
        attempts: int | None = None,
        blocked_member_id: int | None = None,
    ) -> None:
        values: dict[str, object] = {"status": status, "claim_token": None, "claimed_until": None}
        if error is not None:
            values["last_error"] = error
        if sent_at is not None:
            values["sent_at"] = sent_at
        if attempts is not None:
            values["attempts"] = attempts
        async with self._sessions() as session, session.begin():
            fenced = await session.execute(
                update(Notification)
                .where(Notification.id == notification_id, Notification.claim_token == token)
                .values(**values)
            )
            if fenced.rowcount != 1:  # type: ignore[attr-defined]
                log.warning(
                    "notification %s: lease lost before recording %s", notification_id, status.value
                )
            if blocked_member_id is not None:
                await session.execute(
                    update(Member)
                    .where(Member.id == blocked_member_id, Member.bot_blocked_at.is_(None))
                    .values(bot_blocked_at=self._clock.now())
                )

    async def _reschedule(
        self,
        notification_id: int,
        token: str,
        at: datetime,
        *,
        count_attempt: bool,
        error: str | None = None,
    ) -> None:
        values: dict[str, object] = {
            "next_attempt_at": at,
            "claim_token": None,
            "claimed_until": None,
        }
        if count_attempt:
            values["attempts"] = Notification.attempts + 1
        if error is not None:
            values["last_error"] = error
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(Notification)
                .where(Notification.id == notification_id, Notification.claim_token == token)
                .values(**values)
            )

    async def run(self, stop: asyncio.Event, *, idle_interval: float = 1.0) -> None:
        """Send until ``stop`` is set; sleeps when idle and for as long as 429 asks."""
        while not stop.is_set():
            try:
                report = await self.run_once()
            except Exception:
                log.exception("outbox iteration failed")
                report = SendReport(retry_after=idle_interval * 5)
            if report.retry_after is not None:
                delay = report.retry_after
            elif report.claimed == 0:
                delay = idle_interval
            else:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
