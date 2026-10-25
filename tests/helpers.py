from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import select

from paidclub import club
from paidclub.billing.base import (
    ChargeResult,
    Checkout,
    CheckoutRequest,
    PaymentFailed,
    PaymentSucceeded,
    ProviderSubscriptionRef,
    RenewalRequest,
)
from paidclub.billing.fake import CardKind, FakeProvider
from paidclub.container import Core
from paidclub.models import AccessEvent, ClubChat, Notification, Payment, Subscription
from paidclub.notifications import OutgoingMessage
from paidclub.payments import apply_event

OWNER_ID = 1000
ADMIN_ID = 1001
CHAT_ID = -1001234567890


class RecordingSender:
    """A ``send`` callable for the outbox; exceptions can be scripted per chat."""

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.script: dict[int, list[Exception]] = {}

    async def __call__(self, message: OutgoingMessage) -> None:
        errors = self.script.get(message.chat_id)
        if errors:
            raise errors.pop(0)
        self.sent.append(message)


async def checkout_event(
    core: Core,
    fake: FakeProvider,
    telegram_id: int,
    *,
    plan: str = "month",
    card: CardKind | None = "ok",
) -> PaymentSucceeded:
    """Start a checkout and return the success event the fake payment page would send."""
    link = await club.start_checkout(core, telegram_id=telegram_id, plan_code=plan, provider="fake")
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    headers, body = fake.checkout_webhook(session, succeeded=True, card=card)
    [event] = await fake.parse_webhook(headers, body)
    assert isinstance(event, PaymentSucceeded)
    return event


async def subscribe(
    core: Core,
    fake: FakeProvider,
    telegram_id: int,
    *,
    plan: str = "month",
    card: CardKind | None = "ok",
) -> int:
    event = await checkout_event(core, fake, telegram_id, plan=plan, card=card)
    result = await apply_event(core, event)
    assert result.subscription_id is not None
    return result.subscription_id


async def add_invite_link(core: Core, link: str = "https://t.me/+club") -> None:
    async with core.sessions() as session, session.begin():
        session.add(
            ClubChat(chat_id=CHAT_ID, title="Клуб", invite_link=link, updated_at=core.clock.now())
        )


async def load_sub(core: Core, sub_id: int) -> Subscription:
    async with core.sessions() as session:
        sub = await session.get(Subscription, sub_id)
        assert sub is not None
        return sub


async def notifications(core: Core, kind: str | None = None) -> Sequence[Notification]:
    async with core.sessions() as session:
        stmt = select(Notification).order_by(Notification.id)
        if kind is not None:
            stmt = stmt.where(Notification.kind == kind)
        return (await session.scalars(stmt)).all()


async def payments(core: Core) -> Sequence[Payment]:
    async with core.sessions() as session:
        return (await session.scalars(select(Payment).order_by(Payment.id))).all()


async def access_events(core: Core) -> Sequence[AccessEvent]:
    async with core.sessions() as session:
        return (await session.scalars(select(AccessEvent).order_by(AccessEvent.id))).all()


class StubStarsProvider:
    """Stands in for the Telegram Stars provider: Telegram renews, we never charge."""

    name = "stars"
    currency = "XTR"
    supports_merchant_renewal = False

    def __init__(self) -> None:
        self.canceled: list[ProviderSubscriptionRef] = []
        self.resumed: list[ProviderSubscriptionRef] = []

    async def start_checkout(self, request: CheckoutRequest) -> Checkout:
        return Checkout(url=f"https://t.me/$invoice-{request.subscription_id}")

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        raise AssertionError("Telegram renews Stars subscriptions itself")

    async def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Sequence[PaymentSucceeded | PaymentFailed]:
        return []

    async def cancel(self, ref: ProviderSubscriptionRef) -> None:
        self.canceled.append(ref)

    async def resume(self, ref: ProviderSubscriptionRef) -> None:
        self.resumed.append(ref)
