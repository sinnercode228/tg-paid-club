"""Database schema.

All timestamps are stored in UTC through :class:`UTCDateTime`, which also turns SQLite's naive
datetimes back into aware ones, so comparisons never mix naive and aware values.

The constraints carry the guarantees, code only relies on them:

* ``payments (provider, provider_payment_id)`` is unique: the same webhook twice records once.
* ``payments (subscription_id, for_period)`` is unique among applied successful payments: no
  period is paid (and extended) twice, even by two different payments.
* ``subscriptions.member_id`` is unique among live subscriptions: one live subscription per member.
* ``subscriptions.version`` is an optimistic lock: two writers that loaded the same row cannot
  both commit a transition; the loser gets ``StaleDataError`` and retries on fresh data.
* ``charge_attempts.idempotence_key`` and ``notifications.key`` are unique: one charge request
  per (subscription, period, attempt), one message per logical event.

Every status change of a subscription is also appended to ``subscription_events`` by a
``before_flush`` hook, whichever code path made it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from paidclub.billing.base import PaymentPurpose
from paidclub.clock import ensure_utc
from paidclub.domain.subscriptions import LIVE_STATUSES, ExpireReason, SubStatus


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite stores them naive)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        value = ensure_utc(value)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _enum[E: StrEnum](enum_cls: type[E], length: int = 24) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime(),
        SubStatus: _enum(SubStatus),
        ExpireReason: _enum(ExpireReason),
        PaymentPurpose: _enum(PaymentPurpose),
    }


class ClaimMixin:
    """Lease columns for work that several workers may pick up (renewals, revocations, sends).

    A row is free when ``claimed_until`` is empty or in the past; see :func:`paidclub.db.claim`.
    """

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_token: Mapped[str | None] = mapped_column(String(32))
    claimed_until: Mapped[datetime | None]


# --- members and chats -------------------------------------------------------------------------


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime]
    #: Set when Telegram answers 403 to a message: the member blocked the bot.
    bot_blocked_at: Mapped[datetime | None]


class ClubChat(Base):
    """A club channel or group, with the single join-request invite link the bot created."""

    __tablename__ = "club_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str | None] = mapped_column(String(255))
    invite_link: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime]


# --- subscriptions -----------------------------------------------------------------------------


class Subscription(ClaimMixin, Base):
    __tablename__ = "subscriptions"

    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    member: Mapped[Member] = relationship(lazy="joined", innerjoin=True)
    plan_code: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))
    status: Mapped[SubStatus]

    # Snapshot of the plan at purchase time: later price changes do not touch this member.
    period_unit: Mapped[str] = mapped_column(String(8))
    period_count: Mapped[int]
    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))

    anchor_at: Mapped[datetime | None]
    anchor_periods: Mapped[int] = mapped_column(default=0)
    periods_paid: Mapped[int] = mapped_column(default=0)
    current_period_start: Mapped[datetime | None]
    current_period_end: Mapped[datetime | None]
    next_charge_at: Mapped[datetime | None]
    dunning_attempt: Mapped[int] = mapped_column(default=0)
    past_due_since: Mapped[datetime | None]
    grace_until: Mapped[datetime | None]

    #: YooKassa saved payment method id, or the Telegram charge id of a Stars subscription.
    payment_method_id: Mapped[str | None] = mapped_column(String(128))

    activated_at: Mapped[datetime | None]
    canceled_at: Mapped[datetime | None]
    expired_at: Mapped[datetime | None]
    expire_reason: Mapped[ExpireReason | None]
    #: Set once the member has been removed from every club chat after expiry.
    access_revoked_at: Mapped[datetime | None]

    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[int] = mapped_column(default=1)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    __table_args__ = (
        Index("ix_subscriptions_due", "status", "next_charge_at"),
        Index("ix_subscriptions_period_end", "status", "current_period_end"),
    )


class SubscriptionEvent(Base):
    """Every status change of a subscription, written automatically on flush.

    Subscription rows only hold the current state (a reactivation clears ``expired_at``, a
    resume clears ``canceled_at``), so churn and cancellation counts over time come from here.
    """

    __tablename__ = "subscription_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    subscription: Mapped[Subscription] = relationship(lazy="raise")
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    plan_code: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))
    from_status: Mapped[SubStatus | None]
    to_status: Mapped[SubStatus]
    reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(index=True)


@event.listens_for(Session, "before_flush")
def _log_status_changes(session: Session, _context: object, _instances: object) -> None:
    for obj in [*session.new, *session.dirty]:
        if not isinstance(obj, Subscription):
            continue
        history = inspect(obj).attrs.status.history
        if not history.added:
            continue
        before = history.deleted[0] if history.deleted else None
        if before == obj.status:
            continue
        reason = None
        if obj.status is SubStatus.EXPIRED and obj.expire_reason is not None:
            reason = obj.expire_reason.value
        session.add(
            SubscriptionEvent(
                subscription=obj,
                member_id=obj.member_id,
                plan_code=obj.plan_code,
                provider=obj.provider,
                from_status=before,
                to_status=obj.status,
                reason=reason,
                created_at=obj.updated_at,
            )
        )


_live = Subscription.status.in_(LIVE_STATUSES)
Index(
    "uq_subscriptions_live_member",
    Subscription.member_id,
    unique=True,
    sqlite_where=_live,
    postgresql_where=_live,
)


class PaymentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PaymentOutcome(StrEnum):
    #: Changed the subscription: a period was paid, or a failed renewal moved dunning on.
    APPLIED = "applied"
    #: Kept for the books without a state change (a failed first checkout, for example).
    RECORDED = "recorded"
    #: A failure for a period or attempt that is already settled.
    STALE = "stale"
    #: Money arrived but there is nothing to apply it to (period already paid, member already
    #: subscribed). The owner gets an alert to refund it.
    NEEDS_REFUND = "needs_refund"
    AMOUNT_MISMATCH = "amount_mismatch"
    UNKNOWN_SUBSCRIPTION = "unknown_subscription"
    #: The same provider payment was processed before; nothing was written this time.
    DUPLICATE = "duplicate"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    provider_payment_id: Mapped[str] = mapped_column(String(128))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    plan_code: Mapped[str | None] = mapped_column(String(32))
    purpose: Mapped[PaymentPurpose]
    status: Mapped[PaymentStatus] = mapped_column(_enum(PaymentStatus))
    outcome: Mapped[PaymentOutcome] = mapped_column(_enum(PaymentOutcome))
    applied: Mapped[bool] = mapped_column(default=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    for_period: Mapped[int | None]
    attempt: Mapped[int | None]
    period_start: Mapped[datetime | None]
    period_end: Mapped[datetime | None]
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    #: The provider's own timestamp, for reference.
    occurred_at: Mapped[datetime]
    #: When we processed it; stats are bucketed by this.
    created_at: Mapped[datetime] = mapped_column(index=True)

    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_id"),
    )


_paid = (Payment.status == PaymentStatus.SUCCEEDED) & Payment.applied.is_(True)
Index(
    "uq_payments_paid_period",
    Payment.subscription_id,
    Payment.for_period,
    unique=True,
    sqlite_where=_paid,
    postgresql_where=_paid,
)


class Refund(Base):
    """Money that went back to a member. Recorded once per provider refund id; revenue in the
    stats stays gross."""

    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    refund_id: Mapped[str] = mapped_column(String(128))
    #: The refunded payment as the provider names it, and our row for it if we have one.
    provider_payment_id: Mapped[str] = mapped_column(String(128))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    occurred_at: Mapped[datetime]
    created_at: Mapped[datetime]

    __table_args__ = (UniqueConstraint("provider", "refund_id", name="uq_refunds_provider_id"),)


class ChargeAttemptStatus(StrEnum):
    #: Row written, provider call not finished (or the worker died mid-call).
    STARTED = "started"
    #: The provider accepted the charge but has not decided yet.
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ChargeAttempt(Base):
    """One merchant-initiated renewal charge. Written before the provider call, so a crash
    mid-charge is retried with the same idempotence key instead of charging twice. A charge
    the provider reports as pending is looked up by its payment id until it is decided, even
    if the subscription has moved on meanwhile (the member cancelled)."""

    __tablename__ = "charge_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    for_period: Mapped[int]
    attempt: Mapped[int]
    idempotence_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[ChargeAttemptStatus] = mapped_column(_enum(ChargeAttemptStatus))
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), index=True)
    calls: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    #: For a pending charge: when to ask the provider for its decision again.
    next_check_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    __table_args__ = (
        UniqueConstraint("subscription_id", "for_period", "attempt", name="uq_charge_attempt"),
        Index("ix_charge_attempts_pending", "status", "next_check_at"),
    )


class Checkout(Base):
    """A payment page or invoice the member was sent to, when the provider created a payment
    for it up front (YooKassa, the fake provider).

    A first payment is otherwise applied only when its notification arrives, and YooKassa
    stops retrying a notification after 24 hours. The scheduler looks each unsettled one up
    until the provider decides; the notification, if it comes, settles it the same way.
    """

    __tablename__ = "checkouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    provider: Mapped[str] = mapped_column(String(16))
    provider_payment_id: Mapped[str] = mapped_column(String(128))
    for_period: Mapped[int]
    #: When to look the payment up next; ``None`` once settled or given up.
    next_check_at: Mapped[datetime | None]
    checks: Mapped[int] = mapped_column(default=0)
    #: The payment outcome that settled it, or ``untracked`` / ``abandoned``.
    result: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime]
    settled_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_checkouts_provider_id"),
        Index("ix_checkouts_due", "next_check_at"),
    )


class StarsPaymentStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    #: Still not applied after every retry; the owner was alerted.
    FAILED = "failed"


class StarsPaymentUpdate(Base):
    """A Telegram Stars ``successful_payment`` exactly as it arrived, stored before it is
    applied.

    Telegram delivers the update once and the bot acknowledges it before handling it, so a
    database error while applying would otherwise lose the payment. The scheduler applies
    rows that are still pending.
    """

    __tablename__ = "stars_payment_updates"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    #: ``SuccessfulPayment`` as JSON.
    payment: Mapped[dict[str, Any]] = mapped_column(JSON)
    paid_at: Mapped[datetime]
    status: Mapped[StarsPaymentStatus] = mapped_column(_enum(StarsPaymentStatus))
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime]
    applied_at: Mapped[datetime | None]

    __table_args__ = (Index("ix_stars_payment_updates_due", "status", "next_attempt_at"),)


# --- notifications and access ------------------------------------------------------------------


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    #: Telegram refused for good (bot blocked, chat gone).
    UNDELIVERABLE = "undeliverable"
    #: Gave up after ``outbox_max_attempts`` transient errors.
    FAILED = "failed"
    #: Not sent before ``not_after``: a reminder about a date that has passed.
    STALE = "stale"


class Notification(ClaimMixin, Base):
    """Outbox row. ``key`` names the logical event, so it is enqueued at most once."""

    __tablename__ = "notifications"

    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    text: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    status: Mapped[NotificationStatus] = mapped_column(_enum(NotificationStatus))
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime]
    not_after: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]

    __table_args__ = (Index("ix_notifications_due", "status", "next_attempt_at"),)


class AccessEventKind(StrEnum):
    JOIN_APPROVED = "join_approved"
    JOIN_DECLINED = "join_declined"
    #: Telegram did not accept the approve or decline, even after retries.
    JOIN_FAILED = "join_failed"
    REMOVED = "removed"
    #: Joined without a join request (another link, added by someone) and without access.
    REMOVED_ON_JOIN = "removed_on_join"
    NOT_PARTICIPANT = "not_participant"
    SKIPPED_ADMIN = "skipped_admin"
    SKIPPED_HAS_ACCESS = "skipped_has_access"
    NO_RIGHTS = "no_rights"
    REVOKE_ERROR = "revoke_error"


class AccessEvent(Base):
    """Audit log of every access decision and removal attempt in club chats."""

    __tablename__ = "access_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[AccessEventKind] = mapped_column(_enum(AccessEventKind))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(index=True)
