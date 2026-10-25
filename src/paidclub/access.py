"""Who may be in the club chats: join-request decisions and removal after expiry.

Access is granted through one invite link per chat created with ``creates_join_request=True``.
The bot passes every ``chat_join_request`` to :func:`decide_join_request` and approves or
declines accordingly, so a forwarded link is useless to someone without a subscription.

Removal goes through an :class:`AccessRevoker`, which the bot implements with
``banChatMember`` followed by ``unbanChatMember(only_if_banned=True)``: the member is out, but
not on a ban list, so the same link works again after they pay. The scheduler calls
:func:`revoke_expired_access`; a subscription is only marked as revoked once every chat has
given a final answer, so a network error or a crash means "try again next tick".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Literal, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import texts
from paidclub.container import Core
from paidclub.db import claim, release
from paidclub.domain import subscriptions as domain
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import AccessEvent, AccessEventKind, Subscription
from paidclub.notifications import enqueue
from paidclub.repo import live_subscription, state_of, upsert_member

log = logging.getLogger(__name__)


class RevokeOutcome(StrEnum):
    #: Banned and unbanned: out of the chat, free to rejoin after paying.
    REMOVED = "removed"
    #: Was not in the chat (left on their own, or never joined).
    NOT_PARTICIPANT = "not_participant"
    #: Chat owner or administrator; the bot does not touch them.
    IS_ADMIN = "is_admin"
    #: The bot lacks the "ban users" right in this chat. Recorded; the owner has to fix it.
    NO_RIGHTS = "no_rights"


_OUTCOME_EVENTS: dict[RevokeOutcome, AccessEventKind] = {
    RevokeOutcome.REMOVED: AccessEventKind.REMOVED,
    RevokeOutcome.NOT_PARTICIPANT: AccessEventKind.NOT_PARTICIPANT,
    RevokeOutcome.IS_ADMIN: AccessEventKind.SKIPPED_ADMIN,
    RevokeOutcome.NO_RIGHTS: AccessEventKind.NO_RIGHTS,
}


class AccessRevoker(Protocol):
    async def revoke(self, chat_id: int, user_id: int) -> RevokeOutcome:
        """Remove ``user_id`` from ``chat_id`` so they can rejoin later.

        Return an outcome for every final answer (including "not a participant" and "not
        enough rights"); raise for transient errors (network, 429, 5xx) to be retried.
        """
        ...


@dataclass(frozen=True, slots=True)
class JoinDecision:
    action: Literal["approve", "decline", "ignore"]
    subscription_id: int | None = None
    reason: str = ""


async def record_access_event(
    session: AsyncSession,
    core: Core,
    *,
    kind: AccessEventKind,
    chat_id: int,
    telegram_id: int,
    member_id: int | None = None,
    subscription_id: int | None = None,
    detail: str | None = None,
) -> None:
    session.add(
        AccessEvent(
            kind=kind,
            chat_id=chat_id,
            telegram_id=telegram_id,
            member_id=member_id,
            subscription_id=subscription_id,
            detail=detail,
            created_at=core.clock.now(),
        )
    )


async def access_subscription(
    session: AsyncSession, core: Core, member_id: int
) -> Subscription | None:
    """The member's live subscription if it currently grants access."""
    sub = await live_subscription(session, member_id)
    if sub is not None and domain.has_access(state_of(sub), core.clock.now()):
        return sub
    return None


async def decide_join_request(
    core: Core,
    *,
    chat_id: int,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> JoinDecision:
    """Approve members whose subscription grants access right now, decline everyone else.

    Requests to chats that are not club chats are ignored (the bot may admin other chats).
    The decision is recorded in ``access_events``; the caller performs it and, on decline,
    sends the member the plans.
    """
    if chat_id not in core.settings.club_chat_ids:
        return JoinDecision("ignore", reason="not a club chat")
    now = core.clock.now()
    async with core.sessions() as session, session.begin():
        member = await upsert_member(
            session,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            now=now,
            in_private_chat=False,
        )
        if telegram_id in core.settings.protected_user_ids:
            decision = JoinDecision("approve", reason="owner or admin")
        else:
            sub = await access_subscription(session, core, member.id)
            if sub is not None:
                decision = JoinDecision("approve", sub.id, reason=sub.status.value)
            else:
                decision = JoinDecision("decline", reason="no active subscription")
        await record_access_event(
            session,
            core,
            kind=(
                AccessEventKind.JOIN_APPROVED
                if decision.action == "approve"
                else AccessEventKind.JOIN_DECLINED
            ),
            chat_id=chat_id,
            telegram_id=telegram_id,
            member_id=member.id,
            subscription_id=decision.subscription_id,
            detail=decision.reason,
        )
    return decision


@dataclass
class RevokeReport:
    subscriptions: int = 0
    removed: int = 0
    skipped: int = 0
    errors: int = 0


async def revoke_expired_access(
    core: Core, revoker: AccessRevoker, *, batch_size: int = 50, max_batches: int = 100
) -> RevokeReport:
    """Remove members of expired subscriptions from every club chat, once."""
    report = RevokeReport()
    settings = core.settings
    retry_in = timedelta(seconds=settings.provider_retry_seconds)
    for _ in range(max_batches):
        now = core.clock.now()
        async with core.sessions() as session, session.begin():
            token, ids = await claim(
                session,
                Subscription,
                Subscription.status == SubStatus.EXPIRED,
                Subscription.access_revoked_at.is_(None),
                now=now,
                lease=settings.claim_lease,
                limit=batch_size,
                order_by=Subscription.id,
            )
            subs = (
                (await session.scalars(select(Subscription).where(Subscription.id.in_(ids)))).all()
                if ids
                else []
            )
            targets = [(sub.id, sub.member_id, sub.member.telegram_id) for sub in subs]
        if not targets:
            break

        for sub_id, member_id, telegram_id in targets:
            report.subscriptions += 1
            events: list[tuple[AccessEventKind, int, str | None]] = []
            transient = False
            async with core.sessions() as session:
                has_access = await access_subscription(session, core, member_id) is not None
            if telegram_id in settings.protected_user_ids:
                events = [
                    (AccessEventKind.SKIPPED_ADMIN, chat, "protected user id")
                    for chat in settings.club_chat_ids
                ]
            elif has_access:
                # Paid again through a new subscription before removal ran: leave them in.
                events = [
                    (AccessEventKind.SKIPPED_HAS_ACCESS, chat, None)
                    for chat in settings.club_chat_ids
                ]
            else:
                for chat in settings.club_chat_ids:
                    try:
                        outcome = await revoker.revoke(chat, telegram_id)
                    except Exception as exc:
                        transient = True
                        events.append(
                            (AccessEventKind.REVOKE_ERROR, chat, f"{type(exc).__name__}: {exc}")
                        )
                        log.warning("revoke %s from %s failed: %s", telegram_id, chat, exc)
                    else:
                        events.append((_OUTCOME_EVENTS[outcome], chat, None))
            async with core.sessions() as session, session.begin():
                for kind, chat, detail in events:
                    await record_access_event(
                        session,
                        core,
                        kind=kind,
                        chat_id=chat,
                        telegram_id=telegram_id,
                        member_id=member_id,
                        subscription_id=sub_id,
                        detail=detail,
                    )
                    if kind is AccessEventKind.REMOVED:
                        report.removed += 1
                    elif kind is AccessEventKind.REVOKE_ERROR:
                        report.errors += 1
                    else:
                        report.skipped += 1
                    if kind is AccessEventKind.NO_RIGHTS:
                        await _alert_missing_rights(session, core, chat)
                if not transient:
                    # Conditional on still being expired: a late payment may have reactivated
                    # the subscription meanwhile, and then its next expiry must revoke again.
                    await session.execute(
                        update(Subscription)
                        .where(
                            Subscription.id == sub_id,
                            Subscription.claim_token == token,
                            Subscription.status == SubStatus.EXPIRED,
                        )
                        .values(access_revoked_at=core.clock.now())
                    )
                await release(
                    session,
                    Subscription,
                    sub_id,
                    token,
                    until=core.clock.now() + retry_in if transient else None,
                )
    return report


async def _alert_missing_rights(session: AsyncSession, core: Core, chat_id: int) -> None:
    """Tell the owner once a day per chat that removals there are not happening."""
    log.warning("bot cannot remove members from chat %s: missing admin rights", chat_id)
    if core.settings.owner_id is None:
        return
    now = core.clock.now()
    await enqueue(
        session,
        key=f"owner:no_rights:{chat_id}:{now:%Y-%m-%d}",
        kind="owner_alert",
        chat_id=core.settings.owner_id,
        message=texts.owner_alert(
            f"В чате {chat_id} у бота нет права блокировать участников, поэтому участники "
            "с закончившейся подпиской остаются в чате. Выдайте боту это право; кого не "
            "удалось удалить — видно в журнале доступа."
        ),
        now=now,
    )
