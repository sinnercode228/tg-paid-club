from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import (
    AnswerPreCheckoutQuery,
    CreateInvoiceLink,
    EditUserStarSubscription,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import LabeledPrice, SuccessfulPayment, Update, User
from pydantic import SecretStr
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from paidclub import club
from paidclub.billing import stars as stars_module
from paidclub.billing.base import (
    CheckoutRequest,
    PaymentPurpose,
    PaymentSucceeded,
    ProviderError,
    WebhookRejected,
)
from paidclub.billing.fake import FakeProvider
from paidclub.billing.stars import (
    ERROR_ALREADY_PAID,
    ERROR_AMOUNT,
    ERROR_HAS_SUBSCRIPTION,
    ERROR_INVALID,
    ERROR_STALE,
    ERROR_UNAVAILABLE,
    InvoicePayload,
    PaymentNotStored,
    StarsProvider,
    check_pre_checkout,
    create_stars_router,
    payment_event,
)
from paidclub.bot import member, screens
from paidclub.bot.catalog import catalog
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import PaymentOutcome, StarsPaymentStatus, StarsPaymentUpdate
from paidclub.money import Money
from paidclub.runner import build_app
from paidclub.scheduler import Scheduler
from tests.conftest import START
from tests.helpers import FakeRevoker, load_sub, notifications, payments, subscribe

MEMBER = 901
STRANGER = 902
INVOICE_LINK = "https://t.me/$demo-invoice"
THIRTY_DAYS = timedelta(days=30)


class RecordingSession(BaseSession):
    """Answers Bot API calls from memory and records them.

    Answers go through aiogram's own ``check_response``, so errors arrive as the same
    exceptions as in production (``TelegramBadRequest`` for a 400, and so on).
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.results: dict[str, Any] = {"createInvoiceLink": INVOICE_LINK}
        self.errors: dict[str, list[tuple[int, str] | Exception]] = {}

    def of[M](self, kind: type[M]) -> list[M]:
        return [call for call in self.calls if isinstance(call, kind)]

    def fail(self, api_method: str, error: tuple[int, str] | Exception) -> None:
        self.errors.setdefault(api_method, []).append(error)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram's signature
    ) -> TelegramType:
        self.calls.append(method)
        name = method.__api_method__
        queued = self.errors.get(name)
        if queued:
            error = queued.pop(0)
            if isinstance(error, Exception):
                raise error
            status, description = error
            body = {"ok": False, "error_code": status, "description": description}
            self.check_response(bot, method, status, json.dumps(body))
            raise AssertionError("check_response should have raised")
        if name == "sendMessage":
            assert isinstance(method, SendMessage)
            result: Any = {
                "message_id": len(self.calls),
                "date": int(START.timestamp()),
                "chat": {"id": method.chat_id, "type": "private"},
                "text": method.text,
            }
        else:
            result = self.results.get(name, True)
        response = self.check_response(bot, method, 200, json.dumps({"ok": True, "result": result}))
        assert response.result is not None
        return response.result

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - aiogram's signature
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
async def bot(session: RecordingSession) -> AsyncIterator[Bot]:
    instance = Bot("42:TEST-TOKEN", session=session)
    yield instance
    await instance.session.close()


@pytest.fixture
def stars(core: Core, bot: Bot) -> StarsProvider:
    provider = StarsProvider(bot)
    core.providers.register(provider)
    return provider


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def dispatcher(core: Core, stars: StarsProvider) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(create_stars_router(core, sleep=no_sleep))
    return dp


_update_ids = iter(range(1, 1_000_000))


def pre_checkout_update(
    payload: str, *, user_id: int = MEMBER, amount: int = 500, currency: str = "XTR"
) -> Update:
    return Update.model_validate(
        {
            "update_id": next(_update_ids),
            "pre_checkout_query": {
                "id": f"q{user_id}",
                "from": {"id": user_id, "is_bot": False, "first_name": "Аня"},
                "currency": currency,
                "total_amount": amount,
                "invoice_payload": payload,
            },
        }
    )


def payment_update(
    payload: str,
    *,
    charge_id: str,
    expires: datetime,
    first: bool,
    user_id: int = MEMBER,
    amount: int = 500,
    update_id: int | None = None,
    paid_at: datetime = START,
) -> Update:
    payment: dict[str, Any] = {
        "currency": "XTR",
        "total_amount": amount,
        "invoice_payload": payload,
        "telegram_payment_charge_id": charge_id,
        "provider_payment_charge_id": "",
        "subscription_expiration_date": int(expires.timestamp()),
        "is_recurring": True,
    }
    if first:
        payment["is_first_recurring"] = True
    return Update.model_validate(
        {
            "update_id": update_id if update_id is not None else next(_update_ids),
            "message": {
                "message_id": 100,
                "date": int(paid_at.timestamp()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Аня"},
                "successful_payment": payment,
            },
        }
    )


async def stars_invoice(core: Core, session: RecordingSession) -> tuple[int, str]:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="stars")
    assert link.url == INVOICE_LINK
    payload = session.of(CreateInvoiceLink)[-1].payload
    return link.subscription_id, payload


async def paid_stars_subscription(
    core: Core, session: RecordingSession, dp: Dispatcher, bot: Bot, clock: FakeClock
) -> tuple[int, str, datetime]:
    sub_id, payload = await stars_invoice(core, session)
    expires = clock.now() + THIRTY_DAYS
    await dp.feed_update(
        bot, payment_update(payload, charge_id="ch_1", expires=expires, first=True)
    )
    return sub_id, payload, expires


# --- the invoice -------------------------------------------------------------------------------


async def test_the_invoice_is_a_monthly_stars_subscription(
    core: Core, session: RecordingSession, stars: StarsProvider
) -> None:
    sub_id, payload = await stars_invoice(core, session)

    [call] = session.of(CreateInvoiceLink)
    assert call.__api_method__ == "createInvoiceLink"
    assert call.currency == "XTR"
    assert call.subscription_period == 2592000
    assert call.prices == [LabeledPrice(label="1 месяц", amount=500)]
    assert call.provider_token is None
    assert payload == f"paidclub:{sub_id}:1"
    assert 1 <= len(call.title) <= 32
    assert 1 <= len(call.description) <= 255
    assert len(payload.encode()) <= 128


@pytest.mark.parametrize(
    ("amount", "message"),
    [(Money(99000, "RUB"), "XTR"), (Money(10_001, "XTR"), "10000"), (Money(0, "XTR"), "1 to")],
)
async def test_prices_telegram_would_reject_are_refused_up_front(
    stars: StarsProvider, session: RecordingSession, amount: Money, message: str
) -> None:
    request = CheckoutRequest(
        subscription_id=1,
        member_telegram_id=MEMBER,
        plan_code="month",
        plan_title="1 месяц",
        amount=amount,
        for_period=1,
        description="Подписка",
    )
    with pytest.raises(ValueError, match=message):
        await stars.start_checkout(request)
    assert session.calls == []


async def test_telegram_errors_while_creating_the_invoice_are_provider_errors(
    core: Core, session: RecordingSession, stars: StarsProvider
) -> None:
    session.fail("createInvoiceLink", (400, "Bad Request: SUBSCRIPTION_PERIOD_INVALID"))
    with pytest.raises(ProviderError, match="SUBSCRIPTION_PERIOD_INVALID"):
        await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="stars")


async def test_stars_has_no_webhooks_and_no_merchant_charges(stars: StarsProvider) -> None:
    assert stars.supports_merchant_renewal is False
    with pytest.raises(WebhookRejected):
        await stars.parse_webhook({}, b"{}")


# --- turning payments into events --------------------------------------------------------------


def successful_payment(**changes: Any) -> SuccessfulPayment:
    values: dict[str, Any] = {
        "currency": "XTR",
        "total_amount": 500,
        "invoice_payload": "paidclub:7:1",
        "telegram_payment_charge_id": "ch_1",
        "provider_payment_charge_id": "",
        "subscription_expiration_date": int((START + THIRTY_DAYS).timestamp()),
        "is_recurring": True,
        "is_first_recurring": True,
    }
    values.update(changes)
    return SuccessfulPayment(**values)


def test_the_first_payment_is_a_checkout_that_stores_the_charge_id() -> None:
    event = payment_event(successful_payment(), occurred_at=START)
    assert event == PaymentSucceeded(
        provider="stars",
        provider_payment_id="ch_1",
        subscription_id=7,
        amount=Money(500, "XTR"),
        occurred_at=START,
        purpose=PaymentPurpose.CHECKOUT,
        for_period=1,
        period_end=START + THIRTY_DAYS,
        payment_method_id="ch_1",
    )


def test_a_recurring_payment_is_a_renewal_with_the_period_end_from_telegram() -> None:
    end = START + 2 * THIRTY_DAYS
    event = payment_event(
        successful_payment(
            telegram_payment_charge_id="ch_2",
            is_first_recurring=None,
            subscription_expiration_date=int(end.timestamp()),
        ),
        occurred_at=START,
    )
    assert event is not None
    assert (event.purpose, event.provider_payment_id) == (PaymentPurpose.RENEWAL, "ch_2")
    assert (event.for_period, event.payment_method_id, event.period_end) == (None, None, end)


@pytest.mark.parametrize(
    "changes",
    [{"invoice_payload": "shop:order:5"}, {"invoice_payload": "paidclub:x:1"}, {"currency": "RUB"}],
)
def test_payments_for_other_invoices_are_not_ours(changes: dict[str, Any]) -> None:
    assert payment_event(successful_payment(**changes), occurred_at=START) is None


def test_payload_round_trip() -> None:
    assert InvoicePayload.decode(InvoicePayload(12, 3).encode()) == InvoicePayload(12, 3)
    assert InvoicePayload.decode("paidclub:12") is None


# --- pre-checkout ------------------------------------------------------------------------------


async def test_the_member_s_own_invoice_passes_pre_checkout(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot
) -> None:
    _, payload = await stars_invoice(core, session)

    await dispatcher.feed_update(bot, pre_checkout_update(payload))

    [answer] = session.of(AnswerPreCheckoutQuery)
    assert (answer.pre_checkout_query_id, answer.ok, answer.error_message) == (
        f"q{MEMBER}",
        True,
        None,
    )


async def test_a_forwarded_invoice_is_declined_for_someone_else(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot
) -> None:
    _, payload = await stars_invoice(core, session)

    await dispatcher.feed_update(bot, pre_checkout_update(payload, user_id=STRANGER))

    [answer] = session.of(AnswerPreCheckoutQuery)
    assert (answer.ok, answer.error_message) == (False, ERROR_INVALID)


async def test_pre_checkout_rejects_wrong_amounts_and_unknown_invoices(
    core: Core, session: RecordingSession, stars: StarsProvider
) -> None:
    sub_id, payload = await stars_invoice(core, session)

    async def check(payload: str, *, amount: int = 500, currency: str = "XTR") -> str | None:
        return await check_pre_checkout(
            core, user_id=MEMBER, currency=currency, total_amount=amount, payload=payload
        )

    assert await check(payload) is None
    assert await check(payload, amount=499) == ERROR_AMOUNT
    assert await check(payload, currency="RUB") == ERROR_AMOUNT
    assert await check(f"paidclub:{sub_id + 100}:1") == ERROR_INVALID
    assert await check("paidclub:nonsense") == ERROR_INVALID


async def test_an_invoice_opened_again_after_paying_is_declined(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    _, payload, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)

    await dispatcher.feed_update(bot, pre_checkout_update(payload))

    [answer] = session.of(AnswerPreCheckoutQuery)
    assert (answer.ok, answer.error_message) == (False, ERROR_ALREADY_PAID)


async def test_an_old_stars_invoice_is_declined_while_another_subscription_is_live(
    core: Core, session: RecordingSession, stars: StarsProvider, fake: FakeProvider
) -> None:
    _, payload = await stars_invoice(core, session)
    await subscribe(core, fake, MEMBER)

    error = await check_pre_checkout(
        core, user_id=MEMBER, currency="XTR", total_amount=500, payload=payload
    )
    assert error == ERROR_HAS_SUBSCRIPTION


async def test_after_expiry_the_old_invoice_is_stale_and_a_new_one_reactivates(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, old_payload, expires = await paid_stars_subscription(
        core, session, dispatcher, bot, clock
    )
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    # A canceled Stars subscription waits for a renewal the member may have turned back on.
    clock.set(expires + timedelta(minutes=59))
    assert (await Scheduler(core, FakeRevoker()).tick()).expired == 0
    ended = expires + timedelta(hours=1)
    clock.set(ended)
    assert (await Scheduler(core, FakeRevoker()).tick()).expired == 1

    async def check(payload: str) -> str | None:
        return await check_pre_checkout(
            core, user_id=MEMBER, currency="XTR", total_amount=500, payload=payload
        )

    assert await check(old_payload) == ERROR_STALE
    await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    payload = session.of(CreateInvoiceLink)[-1].payload
    assert await check(payload) is None

    new_end = ended + THIRTY_DAYS
    await dispatcher.feed_update(
        bot, payment_update(payload, charge_id="ch_back", expires=new_end, first=True)
    )
    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.current_period_start, sub.current_period_end) == (
        SubStatus.ACTIVE,
        ended,
        new_end,
    )
    assert sub.payment_method_id == "ch_back"


async def test_pre_checkout_is_declined_in_time_when_the_check_hangs(
    core: Core,
    session: RecordingSession,
    stars: StarsProvider,
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, payload = await stars_invoice(core, session)

    async def stuck(*args: Any, **kwargs: Any) -> str | None:
        await asyncio.sleep(30)
        return None

    monkeypatch.setattr(stars_module, "check_pre_checkout", stuck)
    dp = Dispatcher()
    dp.include_router(create_stars_router(core, pre_checkout_timeout=0.05))

    await asyncio.wait_for(dp.feed_update(bot, pre_checkout_update(payload)), timeout=2)

    [answer] = session.of(AnswerPreCheckoutQuery)
    assert (answer.ok, answer.error_message) == (False, ERROR_UNAVAILABLE)


async def test_invoices_that_are_not_ours_are_left_to_other_handlers(
    session: RecordingSession, dispatcher: Dispatcher, bot: Bot
) -> None:
    await dispatcher.feed_update(bot, pre_checkout_update("shop:order:5"))
    assert session.of(AnswerPreCheckoutQuery) == []


# --- payments ----------------------------------------------------------------------------------


async def test_the_first_payment_activates_until_telegram_s_expiration_date(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, _, expires = await paid_stars_subscription(core, session, dispatcher, bot, clock)

    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert (sub.current_period_start, sub.current_period_end) == (START, expires)
    assert sub.payment_method_id == "ch_1"
    [payment] = await payments(core)
    assert (payment.provider, payment.provider_payment_id) == ("stars", "ch_1")
    assert (payment.amount_minor, payment.currency, payment.for_period) == (500, "XTR", 1)
    assert len(await notifications(core, "activated")) == 1
    assert session.of(EditUserStarSubscription) == []


async def test_the_same_payment_update_twice_pays_once(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, payload = await stars_invoice(core, session)
    update = payment_update(payload, charge_id="ch_1", expires=START + THIRTY_DAYS, first=True)

    await dispatcher.feed_update(bot, update)
    await dispatcher.feed_update(bot, update)

    assert len(await payments(core)) == 1
    assert (await load_sub(core, sub_id)).periods_paid == 1
    assert session.of(EditUserStarSubscription) == []


async def test_a_renewal_extends_to_the_new_expiration_date(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, payload, expires = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    clock.set(expires)
    renewed_until = expires + THIRTY_DAYS

    await dispatcher.feed_update(
        bot, payment_update(payload, charge_id="ch_2", expires=renewed_until, first=False)
    )

    sub = await load_sub(core, sub_id)
    assert (sub.periods_paid, sub.current_period_start, sub.current_period_end) == (
        2,
        expires,
        renewed_until,
    )
    assert sub.payment_method_id == "ch_1"
    renewal = (await payments(core))[-1]
    assert (renewal.purpose, renewal.outcome) == (PaymentPurpose.RENEWAL, PaymentOutcome.APPLIED)
    assert len(await notifications(core, "renewed")) == 1


async def test_a_second_telegram_subscription_from_the_same_invoice_is_flagged_and_stopped(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, payload, expires = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    clock.advance(timedelta(minutes=2))

    # Paid twice before the first payment was applied (both pre-checks passed): Telegram
    # started a second subscription that ends two minutes later.
    await dispatcher.feed_update(
        bot,
        payment_update(
            payload, charge_id="ch_dup", expires=expires + timedelta(minutes=2), first=True
        ),
    )

    sub = await load_sub(core, sub_id)
    assert (sub.periods_paid, sub.current_period_end, sub.payment_method_id) == (1, expires, "ch_1")
    duplicate = (await payments(core))[-1]
    assert duplicate.outcome is PaymentOutcome.NEEDS_REFUND
    [stop] = session.of(EditUserStarSubscription)
    assert (stop.user_id, stop.telegram_payment_charge_id, stop.is_canceled) == (
        MEMBER,
        "ch_dup",
        True,
    )
    [alert] = await notifications(core, "owner_alert")
    assert "ch_dup" in alert.text
    [notice] = session.of(SendMessage)
    assert notice.chat_id == MEMBER
    assert "уже оплачен" in notice.text


async def test_paying_again_after_a_missed_renewal_stops_the_old_telegram_subscription(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, _, expires = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    clock.set(expires + timedelta(hours=2))
    report = await Scheduler(core, FakeRevoker()).tick()
    assert report.renewals_missing == 1
    assert (await load_sub(core, sub_id)).status is SubStatus.PAST_DUE

    link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    assert link.url == INVOICE_LINK
    payload = session.of(CreateInvoiceLink)[-1].payload
    assert payload == f"paidclub:{sub_id}:2"
    await dispatcher.feed_update(bot, pre_checkout_update(payload))
    assert session.of(AnswerPreCheckoutQuery)[-1].ok is True
    new_end = clock.now() + THIRTY_DAYS
    await dispatcher.feed_update(
        bot, payment_update(payload, charge_id="ch_new", expires=new_end, first=True)
    )

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid, sub.current_period_end) == (
        SubStatus.ACTIVE,
        2,
        new_end,
    )
    assert sub.payment_method_id == "ch_new"
    [stop] = session.of(EditUserStarSubscription)
    assert (stop.telegram_payment_charge_id, stop.is_canceled) == ("ch_1", True)


# --- cancel and resume -------------------------------------------------------------------------


async def test_cancel_and_resume_edit_the_telegram_subscription(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, _, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)

    canceled = await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    resumed = await club.resume(core, telegram_id=MEMBER, subscription_id=sub_id)

    # Telegram only allows the member to turn renewal back on: until a renewal arrives,
    # auto-renew is off on our side too.
    assert (canceled.status, resumed.status) == (SubStatus.CANCELED, SubStatus.CANCELED)
    stop, allow = session.of(EditUserStarSubscription)
    assert (stop.user_id, stop.telegram_payment_charge_id, stop.is_canceled) == (
        MEMBER,
        "ch_1",
        True,
    )
    assert (allow.telegram_payment_charge_id, allow.is_canceled) == ("ch_1", False)


async def test_cancel_goes_ahead_when_telegram_has_nothing_left_to_cancel(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, _, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    session.fail("editUserStarSubscription", (400, "Bad Request: SUBSCRIPTION_NOT_ACTIVE"))

    view = await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)

    assert view.status is SubStatus.CANCELED


async def test_a_network_error_on_cancel_leaves_the_subscription_unchanged(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, _, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    session.fail(
        "editUserStarSubscription",
        TelegramNetworkError(
            method=EditUserStarSubscription(
                user_id=MEMBER, telegram_payment_charge_id="ch_1", is_canceled=True
            ),
            message="timeout",
        ),
    )

    with pytest.raises(ProviderError):
        await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)

    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE


async def test_a_resume_refused_by_telegram_is_reported_not_pretended(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, _, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    session.fail("editUserStarSubscription", (400, "Bad Request: SUBSCRIPTION_EXPIRED"))

    with pytest.raises(ProviderError):
        await club.resume(core, telegram_id=MEMBER, subscription_id=sub_id)

    assert (await load_sub(core, sub_id)).status is SubStatus.CANCELED


async def test_answering_pre_checkout_records_no_payment(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot
) -> None:
    sub_id, payload = await stars_invoice(core, session)
    await dispatcher.feed_update(bot, pre_checkout_update(payload))
    assert await payments(core) == []
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING


# --- Stars switched off after members subscribed ---------------------------------------------


async def test_cancel_reaches_telegram_after_stars_is_switched_off(
    core: Core,
    session: RecordingSession,
    dispatcher: Dispatcher,
    bot: Bot,
    clock: FakeClock,
    settings: Settings,
) -> None:
    """The owner drops "stars" from PAIDCLUB_PROVIDERS. Telegram keeps renewing the members
    who subscribed earlier, so turning auto-renew off must still reach Telegram."""
    sub_id, _, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)

    restarted = RecordingSession()
    app = await build_app(
        settings.model_copy(
            update={"bot_token": SecretStr("42:TEST-TOKEN"), "providers": ["fake"]}
        ),
        bot_session=restarted,
        clock=clock,
    )
    try:
        assert "stars" in app.core.providers
        assert [offer.provider for item in catalog(app.core) for offer in item.offers] == [
            "fake",
            "fake",
            "fake",
        ]
        with pytest.raises(club.ProviderUnavailable):
            await club.start_checkout(
                app.core, telegram_id=STRANGER, plan_code="month", provider="stars"
            )

        view = await club.cancel(app.core, telegram_id=MEMBER, subscription_id=sub_id)
        assert view.status is SubStatus.CANCELED
        [stop] = restarted.of(EditUserStarSubscription)
        assert (stop.telegram_payment_charge_id, stop.is_canceled) == ("ch_1", True)
    finally:
        await app.aclose()


async def test_without_the_stars_provider_cancel_is_refused_rather_than_pretended(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    sub_id, _, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    bare = await build_core(core.settings, clock=clock, engine=core.engine)

    with pytest.raises(club.ProviderUnavailable):
        await club.cancel(bare, telegram_id=MEMBER, subscription_id=sub_id)

    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE
    assert session.of(EditUserStarSubscription) == []


# --- turning renewal back on --------------------------------------------------------------------


async def test_resume_asks_the_member_to_turn_renewal_on_in_telegram(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    """editUserStarSubscription(is_canceled=False) only allows the member to re-enable it."""
    sub_id, _, _ = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)

    user = User(id=MEMBER, is_bot=False, first_name="Аня")
    reply = await member.toggle_reply(core, user, sub_id, enable=True)

    [_, allow] = session.of(EditUserStarSubscription)
    assert allow.is_canceled is False
    assert reply.draft.text.startswith(screens.STARS_RESUME_NOTE)
    assert "Telegram сам продлит" not in reply.draft.text
    assert "снова включено" not in reply.draft.text
    assert screens.STARS_RESUME_BUTTON in [button.text for button in reply.draft.buttons]
    assert (await load_sub(core, sub_id)).status is SubStatus.CANCELED


async def test_a_renewal_after_the_cancel_turns_auto_renew_back_on(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    """The member turned renewal back on in Telegram, which renews at the period end."""
    sub_id, payload, expires = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    clock.advance(timedelta(days=10))
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    await club.resume(core, telegram_id=MEMBER, subscription_id=sub_id)

    clock.set(expires + timedelta(minutes=2))
    assert (await Scheduler(core, FakeRevoker()).tick()).expired == 0  # waits for Telegram
    renewed_until = expires + THIRTY_DAYS
    await dispatcher.feed_update(
        bot,
        payment_update(
            payload, charge_id="ch_2", expires=renewed_until, first=False, paid_at=expires
        ),
    )

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.canceled_at, sub.current_period_end) == (
        SubStatus.ACTIVE,
        None,
        renewed_until,
    )
    assert sub.next_charge_at == renewed_until + timedelta(hours=1)


async def test_a_renewal_charged_before_the_cancel_keeps_auto_renew_off(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    """Telegram renewed at the period end and the member cancelled a minute later, before
    the payment update was handled."""
    sub_id, payload, expires = await paid_stars_subscription(core, session, dispatcher, bot, clock)
    clock.set(expires + timedelta(minutes=1))
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    renewed_until = expires + THIRTY_DAYS
    await dispatcher.feed_update(
        bot,
        payment_update(
            payload, charge_id="ch_2", expires=renewed_until, first=False, paid_at=expires
        ),
    )

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.next_charge_at, sub.current_period_end) == (
        SubStatus.CANCELED,
        None,
        renewed_until,
    )


# --- payments that meet a database error --------------------------------------------------------


@contextlib.contextmanager
def failing_inserts(core: Core, table: str, times: int) -> Iterator[list[int]]:
    """``INSERT INTO <table>`` fails ``times`` times with "database is locked"."""
    left = [times]

    def fail(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if left[0] and statement.lstrip().upper().startswith(f"INSERT INTO {table.upper()} "):
            left[0] -= 1
            raise OperationalError(statement, None, Exception("database is locked"))

    event.listen(core.engine.sync_engine, "before_cursor_execute", fail)
    try:
        yield left
    finally:
        event.remove(core.engine.sync_engine, "before_cursor_execute", fail)


async def stored_updates(core: Core) -> list[StarsPaymentUpdate]:
    async with core.sessions() as session:
        rows = await session.scalars(select(StarsPaymentUpdate).order_by(StarsPaymentUpdate.id))
        return list(rows.all())


async def test_a_payment_that_meets_a_database_error_is_applied_by_the_scheduler(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    """Telegram does not deliver successful_payment again, and the update was acknowledged
    already; one failed INSERT INTO payments must not lose the 500 Stars."""
    sub_id, payload = await stars_invoice(core, session)
    expires = clock.now() + THIRTY_DAYS
    with failing_inserts(core, "payments", times=1):
        await dispatcher.feed_update(
            bot, payment_update(payload, charge_id="ch_1", expires=expires, first=True)
        )
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    [row] = await stored_updates(core)
    assert (row.status, row.telegram_payment_charge_id) == (StarsPaymentStatus.PENDING, "ch_1")
    assert row.last_error is not None and "database is locked" in row.last_error

    clock.advance(timedelta(minutes=1))
    assert (await Scheduler(core, FakeRevoker()).tick()).stars_applied == 1

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.current_period_end, sub.payment_method_id) == (
        SubStatus.ACTIVE,
        expires,
        "ch_1",
    )
    [row] = await stored_updates(core)
    assert row.status is StarsPaymentStatus.APPLIED
    clock.advance(timedelta(hours=1))
    assert (await Scheduler(core, FakeRevoker()).tick()).stars_applied == 0


async def test_a_payment_that_cannot_be_stored_reaches_the_owner_outside_the_database(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    _, payload = await stars_invoice(core, session)
    update_ = payment_update(
        payload, charge_id="ch_1", expires=clock.now() + THIRTY_DAYS, first=True
    )
    with failing_inserts(core, "stars_payment_updates", times=10), pytest.raises(PaymentNotStored):
        await dispatcher.feed_update(bot, update_)

    [alert] = [call for call in session.of(SendMessage) if call.chat_id == core.settings.owner_id]
    assert "ch_1" in alert.text and "500" in alert.text
    assert await stored_updates(core) == []


async def test_a_stored_payment_that_never_applies_is_given_up_with_an_owner_alert(
    core: Core, session: RecordingSession, dispatcher: Dispatcher, bot: Bot, clock: FakeClock
) -> None:
    _, payload = await stars_invoice(core, session)
    with failing_inserts(core, "payments", times=100):
        await dispatcher.feed_update(
            bot,
            payment_update(
                payload, charge_id="ch_1", expires=clock.now() + THIRTY_DAYS, first=True
            ),
        )
        for _ in range(stars_module.APPLY_ATTEMPTS + 2):
            clock.advance(timedelta(hours=1))
            await Scheduler(core, FakeRevoker()).tick()

    [row] = await stored_updates(core)
    assert (row.status, row.attempts) == (StarsPaymentStatus.FAILED, stars_module.APPLY_ATTEMPTS)
    [alert] = await notifications(core, "owner_alert")
    assert "ch_1" in alert.text
