"""Small shared queries and the mapping between ``Subscription`` rows and domain states."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub.domain.subscriptions import LIVE_STATUSES, PeriodUnit, SubState
from paidclub.models import ClubChat, Member, Subscription
from paidclub.money import Money

_STATE_FIELDS = (
    "status",
    "anchor_at",
    "anchor_periods",
    "periods_paid",
    "current_period_start",
    "current_period_end",
    "next_charge_at",
    "dunning_attempt",
    "past_due_since",
    "grace_until",
    "activated_at",
    "canceled_at",
    "expired_at",
    "expire_reason",
)


def state_of(sub: Subscription) -> SubState:
    return SubState(
        period_unit=cast(PeriodUnit, sub.period_unit),
        period_count=sub.period_count,
        **{name: getattr(sub, name) for name in _STATE_FIELDS},
    )


def write_state(sub: Subscription, state: SubState, *, now: datetime) -> None:
    """Copy a new domain state onto the row (only changed columns are written)."""
    for name in _STATE_FIELDS:
        value = getattr(state, name)
        if getattr(sub, name) != value:
            setattr(sub, name, value)
    sub.updated_at = now


def price_of(sub: Subscription) -> Money:
    return Money(sub.price_minor, sub.currency)


async def member_by_telegram_id(session: AsyncSession, telegram_id: int) -> Member | None:
    result = await session.scalars(select(Member).where(Member.telegram_id == telegram_id))
    return result.one_or_none()


async def upsert_member(
    session: AsyncSession,
    *,
    telegram_id: int,
    now: datetime,
    username: str | None = None,
    first_name: str | None = None,
    in_private_chat: bool = True,
) -> Member:
    """Create or refresh a member. ``in_private_chat=False`` for updates that do not prove the
    member can receive messages (a join request), so a recorded block is kept."""
    member = await member_by_telegram_id(session, telegram_id)
    if member is None:
        member = Member(
            telegram_id=telegram_id, username=username, first_name=first_name, created_at=now
        )
        session.add(member)
        await session.flush()
        return member
    if username is not None and member.username != username:
        member.username = username
    if first_name is not None and member.first_name != first_name:
        member.first_name = first_name
    if in_private_chat:
        # Writing to the bot again means it is no longer blocked.
        member.bot_blocked_at = None
    return member


async def live_subscription(
    session: AsyncSession, member_id: int, *, exclude_id: int | None = None
) -> Subscription | None:
    stmt = select(Subscription).where(
        Subscription.member_id == member_id, Subscription.status.in_(LIVE_STATUSES)
    )
    if exclude_id is not None:
        stmt = stmt.where(Subscription.id != exclude_id)
    result = await session.scalars(stmt)
    return result.first()


async def latest_subscription(session: AsyncSession, member_id: int) -> Subscription | None:
    """The live subscription if there is one, otherwise the most recent one."""
    live = await live_subscription(session, member_id)
    if live is not None:
        return live
    result = await session.scalars(
        select(Subscription)
        .where(Subscription.member_id == member_id)
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    return result.first()


async def invite_links(session: AsyncSession) -> list[tuple[str | None, str]]:
    """``(title, invite_link)`` for every club chat the bot has created a link for."""
    result = await session.execute(
        select(ClubChat.title, ClubChat.invite_link)
        .where(ClubChat.invite_link.is_not(None))
        .order_by(ClubChat.chat_id)
    )
    return [(title, link) for title, link in result.all() if link]
