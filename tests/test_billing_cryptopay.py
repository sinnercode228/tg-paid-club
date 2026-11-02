from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from paidclub import club
from paidclub.billing import ProviderRegistry
from paidclub.billing.base import (
    PaymentFailed,
    PaymentPending,
    PaymentPurpose,
    PaymentSucceeded,
    RenewalRequest,
)
from paidclub.billing.cryptopay import (
    TESTNET_API_URL,
    CryptoPayError,
    CryptoPayProvider,
    invoice_event,
    signature,
)
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.domain.subscriptions import SubStatus, renewal_idempotence_key
from paidclub.models import ChargeAttempt, Checkout, PaymentOutcome, PaymentStatus
from paidclub.money import Money
from paidclub.scheduler import Scheduler, TickReport
from paidclub.web.payments import create_webhook_router
from tests.helpers import FakeRevoker, load_sub, notifications, payments

TOKEN = "11111:AAtestCryptoPayToken"
MEMBER = 901
FEB_28 = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
MAR_31 = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _error(status: int, name: str) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "error": {"code": status, "name": name}})


class FakeCryptoPay:
    """An in-memory Crypto Pay API behind ``httpx.MockTransport``.

    It keeps what the provider relies on: the token header, the ``ok``/``result`` envelope,
    ``createInvoice`` for invoices priced in a fiat currency, ``getInvoices`` filtered by
    ``invoice_ids`` answering ``{"items": [...]}``, invoices that go from ``active`` to
    ``paid``, or to ``expired`` once ``expires_in`` has passed, and webhook updates signed the
    way Crypto Pay signs them. ``script`` queues raw responses or transport errors for the next
    requests.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.invoices: dict[int, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response | Exception] = []

    # --- test controls ---------------------------------------------------------------------

    def pay(self, invoice_id: str, *, asset: str = "USDT", paid_amount: str = "10") -> None:
        invoice = self.invoices[int(invoice_id)]
        assert invoice["status"] == "active"
        invoice.update(
            status="paid",
            paid_asset=asset,
            paid_amount=paid_amount,
            paid_fiat_rate="1",
            paid_usd_rate="1",
            fee_asset=asset,
            fee_amount="0.3",
            paid_anonymously=False,
            paid_at=_iso(self.clock.now()),
        )

    def add_foreign_invoice(self) -> str:
        """A paid invoice of the same Crypto Pay app that this app did not create."""
        invoice = self._invoice({"currency_type": "fiat", "fiat": "USD", "amount": "5"})
        invoice.update(payload="order-42", status="paid", paid_at=_iso(self.clock.now()))
        return str(invoice["invoice_id"])

    def update(
        self,
        invoice: dict[str, Any] | str,
        *,
        token: str = TOKEN,
        update_type: str = "invoice_paid",
    ) -> tuple[dict[str, str], bytes]:
        """An update as Crypto Pay posts it: the invoice in ``payload``, signed with ``token``."""
        payload = dict(self.invoices[int(invoice)]) if isinstance(invoice, str) else invoice
        body = json.dumps(
            {
                "update_id": 1,
                "update_type": update_type,
                "request_date": _iso(self.clock.now()),
                "payload": payload,
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        headers["crypto-pay-api-signature"] = signature(token, body)
        return headers, body

    def lookups(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/getInvoices")]

    # --- the API ---------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            scripted = self.script.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        if request.headers.get("Crypto-Pay-API-Token") != TOKEN:
            return _error(401, "UNAUTHORIZED")
        self._expire_overdue()
        params = json.loads(request.content) if request.content else dict(request.url.params)
        method = request.url.path.rsplit("/", 1)[1]
        if method == "createInvoice":
            if params.get("currency_type") != "fiat" or "fiat" not in params:
                return _error(400, "FIAT_REQUIRED")
            return _ok(self._invoice(params))
        if method == "getInvoices":
            wanted = {int(part) for part in str(params.get("invoice_ids", "")).split(",") if part}
            items = [dict(v) for k, v in self.invoices.items() if not wanted or k in wanted]
            return _ok({"items": items})
        return _error(405, "METHOD_NOT_FOUND")

    def _invoice(self, params: dict[str, Any]) -> dict[str, Any]:
        invoice_id = 7000 + len(self.invoices) + 1
        now = self.clock.now()
        invoice: dict[str, Any] = {
            "invoice_id": invoice_id,
            "hash": f"IV{invoice_id}",
            "currency_type": params["currency_type"],
            "fiat": params["fiat"],
            "amount": params["amount"],
            "accepted_assets": str(params.get("accepted_assets", "USDT")).split(","),
            "bot_invoice_url": f"https://t.me/CryptoBot?start=IV{invoice_id}",
            "mini_app_invoice_url": f"https://t.me/CryptoBot/app?startapp=invoice-IV{invoice_id}",
            "web_app_invoice_url": f"https://app.send.tg/invoices/IV{invoice_id}",
            "description": params.get("description"),
            "status": "active",
            "created_at": _iso(now),
            "allow_comments": True,
            "allow_anonymous": True,
            "payload": params.get("payload"),
        }
        if "expires_in" in params:
            invoice["expiration_date"] = _iso(now + timedelta(seconds=params["expires_in"]))
        self.invoices[invoice_id] = invoice
        return invoice

    def _expire_overdue(self) -> None:
        now = _iso(self.clock.now())
        for invoice in self.invoices.values():
            ends = invoice.get("expiration_date")
            if invoice["status"] == "active" and ends is not None and ends <= now:
                invoice["status"] = "expired"


@pytest.fixture
def api(clock: FakeClock) -> FakeCryptoPay:
    return FakeCryptoPay(clock)


@pytest.fixture
async def cryptopay(
    core: Core, api: FakeCryptoPay, clock: FakeClock
) -> AsyncIterator[CryptoPayProvider]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    provider = CryptoPayProvider(
        api_token=TOKEN,
        assets=["USDT", "TON"],
        return_url=core.settings.after_payment_url,
        client=client,
        clock=clock,
    )
    core.providers.register(provider)
    yield provider
    await client.aclose()


@pytest.fixture
async def http(core: Core) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(create_webhook_router(core))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def checkout(core: Core, plan: str = "month") -> tuple[int, str]:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code=plan, provider="cryptopay")
    return link.subscription_id, link.url.rsplit("IV", 1)[1]


async def repay(core: Core, sub_id: int) -> str:
    """The "pay" button: a new invoice for the next period of the subscription."""
    link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    return link.url.rsplit("IV", 1)[1]


async def deliver(api: FakeCryptoPay, http: httpx.AsyncClient, invoice_id: str) -> list[Any]:
    headers, body = api.update(invoice_id)
    response = await http.post("/webhooks/cryptopay", content=body, headers=headers)
    assert response.status_code == 200, response.text
    results: list[Any] = response.json()["results"]
    return results


async def paid_subscription(
    core: Core, api: FakeCryptoPay, http: httpx.AsyncClient
) -> tuple[int, str]:
    sub_id, invoice_id = await checkout(core)
    api.pay(invoice_id)
    assert await deliver(api, http, invoice_id) == [
        {"outcome": "applied", "subscription_id": sub_id}
    ]
    return sub_id, invoice_id


async def tick(core: Core) -> TickReport:
    report = await Scheduler(core, FakeRevoker()).tick()
    assert report.errors == 0
    return report


async def checkout_rows(core: Core) -> list[Checkout]:
    async with core.sessions() as session:
        return list((await session.scalars(select(Checkout).order_by(Checkout.id))).all())


# --- checkout ----------------------------------------------------------------------------------


async def test_checkout_is_an_invoice_in_dollars_payable_in_the_configured_coins(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider
) -> None:
    link = await club.start_checkout(
        core, telegram_id=MEMBER, plan_code="month", provider="cryptopay"
    )

    [request] = api.requests
    assert (request.method, str(request.url)) == ("POST", "https://pay.crypt.bot/api/createInvoice")
    assert request.headers["Crypto-Pay-API-Token"] == TOKEN
    assert json.loads(request.content) == {
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": "10.00",
        "accepted_assets": "USDT,TON",
        "description": "Подписка на клуб: 1 месяц",
        "payload": f"paidclub:{link.subscription_id}:1",
        "expires_in": 3600,
    }
    [invoice_id] = api.invoices
    assert link.url == f"https://t.me/CryptoBot?start=IV{invoice_id}"
    assert (link.provider, link.amount) == ("cryptopay", Money(1000, "USD"))
    # Tracked, so the invoice is looked up if its webhook never comes.
    [row] = await checkout_rows(core)
    assert (row.provider, row.provider_payment_id, row.for_period) == (
        "cryptopay",
        str(invoice_id),
        1,
    )


async def test_settings_choose_the_testnet_the_coins_the_lifetime_and_the_return_button(
    tmp_path: Path, api: FakeCryptoPay, clock: FakeClock
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'crypto.db'}",
        providers=["cryptopay"],
        cryptopay_api_token=TOKEN,  # type: ignore[arg-type]
        cryptopay_api_url=TESTNET_API_URL,
        cryptopay_assets="usdt",  # type: ignore[arg-type]
        cryptopay_invoice_lifetime_minutes=30,
        checkout_return_url="https://t.me/club_demo_bot",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        provider = CryptoPayProvider.from_settings(settings, client=client, clock=clock)
        core = await build_core(settings, clock=clock, providers=ProviderRegistry([provider]))
        try:
            await club.start_checkout(
                core, telegram_id=MEMBER, plan_code="quarter", provider="cryptopay"
            )
        finally:
            await core.aclose()
    [request] = api.requests
    assert str(request.url) == "https://testnet-pay.crypt.bot/api/createInvoice"
    body = json.loads(request.content)
    assert (body["amount"], body["accepted_assets"], body["expires_in"]) == ("25.00", "USDT", 1800)
    assert (body["paid_btn_name"], body["paid_btn_url"]) == (
        "callback",
        "https://t.me/club_demo_bot",
    )


def test_from_settings_needs_a_token(settings: Settings) -> None:
    with pytest.raises(ValueError, match="CRYPTOPAY_API_TOKEN"):
        CryptoPayProvider.from_settings(settings)


@pytest.mark.parametrize(
    "answer",
    [
        _error(400, "ASSET_INVALID"),
        httpx.Response(502, text="<html>Bad Gateway</html>"),
        httpx.ConnectError("connection refused"),
    ],
)
async def test_a_failed_invoice_request_is_not_sent_again(
    core: Core,
    api: FakeCryptoPay,
    cryptopay: CryptoPayProvider,
    answer: httpx.Response | Exception,
) -> None:
    api.script.append(answer)
    with pytest.raises(CryptoPayError):
        await checkout(core)
    # No idempotence key: a repeat could only make a second invoice. The member taps again.
    assert len(api.requests) == 1
    assert await checkout_rows(core) == []


async def test_crypto_pay_errors_carry_their_name(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider
) -> None:
    api.script.append(_error(401, "UNAUTHORIZED"))
    with pytest.raises(CryptoPayError) as caught:
        await cryptopay.get_invoice("7001")
    assert (caught.value.status, caught.value.name) == (401, "UNAUTHORIZED")


# --- webhooks ----------------------------------------------------------------------------------


async def test_a_signed_webhook_applies_the_invoice_as_fetched_from_the_api(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider, http: httpx.AsyncClient
) -> None:
    sub_id, invoice_id = await checkout(core)
    api.pay(invoice_id, asset="TON", paid_amount="2.85")

    assert await deliver(api, http, invoice_id) == [
        {"outcome": "applied", "subscription_id": sub_id}
    ]

    [lookup] = api.lookups()
    assert json.loads(lookup.content) == {"invoice_ids": invoice_id}
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.current_period_end == FEB_28
    assert sub.payment_method_id is None  # nothing can be charged later
    [payment] = await payments(core)
    assert (payment.provider, payment.provider_payment_id) == ("cryptopay", invoice_id)
    # The dollar price is what was paid, whichever coin paid it.
    assert (payment.amount_minor, payment.currency) == (1000, "USD")
    [row] = await checkout_rows(core)
    assert (row.next_check_at, row.result) == (None, "applied")


@pytest.mark.parametrize(
    "tamper",
    ["no signature", "another token", "another body", "not hex"],
)
async def test_webhooks_without_a_valid_signature_are_rejected_before_any_request(
    core: Core,
    api: FakeCryptoPay,
    cryptopay: CryptoPayProvider,
    http: httpx.AsyncClient,
    tamper: str,
) -> None:
    sub_id, invoice_id = await checkout(core)
    api.pay(invoice_id)
    headers, body = api.update(invoice_id)
    if tamper == "no signature":
        del headers["crypto-pay-api-signature"]
    elif tamper == "another token":
        headers, body = api.update(invoice_id, token="22222:AAsomebodyElse")
    elif tamper == "another body":
        body = body.replace(b'"invoice_paid"', b'"invoice_paid" ')
    else:
        headers["crypto-pay-api-signature"] = "zz" * 32
    requests_before = len(api.requests)

    response = await http.post("/webhooks/cryptopay", content=body, headers=headers)

    assert response.status_code == 400
    assert len(api.requests) == requests_before
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING


async def test_the_webhook_body_is_not_trusted(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider, http: httpx.AsyncClient
) -> None:
    """Even a correctly signed body (a leaked token, say) is only trusted for the invoice id."""
    sub_id, invoice_id = await checkout(core)
    forged = {
        **api.invoices[int(invoice_id)],
        "status": "paid",
        "paid_asset": "USDT",
        "paid_amount": "10",
        "paid_at": _iso(FEB_28),
    }
    headers, body = api.update(forged)

    response = await http.post("/webhooks/cryptopay", content=body, headers=headers)

    # Crypto Pay says the invoice is still active, so nothing is applied.
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert len(api.lookups()) == 1
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    assert await payments(core) == []


async def test_a_repeated_webhook_pays_the_period_once(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider, http: httpx.AsyncClient
) -> None:
    sub_id, invoice_id = await paid_subscription(core, api, http)

    assert await deliver(api, http, invoice_id) == [
        {"outcome": "duplicate", "subscription_id": sub_id}
    ]
    sub = await load_sub(core, sub_id)
    assert (sub.periods_paid, sub.current_period_end) == (1, FEB_28)
    assert len(await payments(core)) == 1
    assert len(await notifications(core, "activated")) == 1


async def test_foreign_invoices_and_other_updates_are_ignored(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider, http: httpx.AsyncClient
) -> None:
    foreign = api.add_foreign_invoice()
    assert await deliver(api, http, foreign) == []

    _, invoice_id = await checkout(core)
    headers, body = api.update(invoice_id, update_type="invoice_created")
    requests_before = len(api.requests)
    response = await http.post("/webhooks/cryptopay", content=body, headers=headers)
    assert (response.status_code, response.json()["results"]) == (200, [])
    assert len(api.requests) == requests_before
    assert await payments(core) == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        b'{"update_type": "invoice_paid"}',
        b'{"update_type": "invoice_paid", "payload": {"invoice_id": "1/../getMe"}}',
        b'{"update_type": "invoice_paid", "payload": {"invoice_id": 1.5}}',
    ],
)
async def test_malformed_signed_updates_are_rejected_without_a_request(
    api: FakeCryptoPay, cryptopay: CryptoPayProvider, http: httpx.AsyncClient, body: bytes
) -> None:
    headers = {"crypto-pay-api-signature": signature(TOKEN, body)}
    response = await http.post("/webhooks/cryptopay", content=body, headers=headers)
    assert response.status_code == 400
    assert api.requests == []


async def test_an_update_for_an_invoice_crypto_pay_does_not_have_is_rejected(
    api: FakeCryptoPay, cryptopay: CryptoPayProvider, http: httpx.AsyncClient
) -> None:
    invoice = {"invoice_id": 404404, "status": "paid", "payload": "paidclub:1:1"}
    headers, body = api.update(invoice)
    response = await http.post("/webhooks/cryptopay", content=body, headers=headers)
    assert response.status_code == 400
    assert len(api.lookups()) == 1


async def test_the_webhook_asks_for_a_retry_while_the_api_cannot_confirm(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider, http: httpx.AsyncClient
) -> None:
    sub_id, invoice_id = await checkout(core)
    api.pay(invoice_id)
    headers, body = api.update(invoice_id)

    api.script.append(httpx.ReadTimeout("no answer"))
    first = await http.post("/webhooks/cryptopay", content=body, headers=headers)
    assert first.status_code == 502
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING

    second = await http.post("/webhooks/cryptopay", content=body, headers=headers)
    assert second.json()["results"] == [{"outcome": "applied", "subscription_id": sub_id}]


# --- lookups -----------------------------------------------------------------------------------


async def test_a_lost_webhook_is_recovered_by_looking_the_invoice_up(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider, clock: FakeClock
) -> None:
    sub_id, invoice_id = await checkout(core)
    clock.advance(timedelta(minutes=5))
    report = await tick(core)
    assert (report.checkouts_checked, report.checkouts_recovered) == (1, 0)  # still active

    api.pay(invoice_id)  # ... and the webhook never arrives
    clock.advance(timedelta(minutes=15))
    report = await tick(core)

    assert (report.checkouts_checked, report.checkouts_recovered) == (1, 1)
    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid) == (SubStatus.ACTIVE, 1)
    [alert] = await notifications(core, "owner_alert")
    assert "http://localhost:8000/webhooks/cryptopay" in alert.text
    [row] = await checkout_rows(core)
    assert (row.next_check_at, row.result) == (None, "applied")


async def test_an_expired_invoice_is_a_failed_checkout_that_leaves_the_subscription_alone(
    core: Core, api: FakeCryptoPay, cryptopay: CryptoPayProvider, clock: FakeClock
) -> None:
    sub_id, invoice_id = await checkout(core)
    assert isinstance(await cryptopay.check_payment(invoice_id), PaymentPending)

    clock.advance(timedelta(minutes=61))  # past expires_in
    result = await cryptopay.check_payment(invoice_id)
    assert isinstance(result, PaymentFailed)
    assert (result.reason, result.purpose, result.for_period) == (
        "expired",
        PaymentPurpose.CHECKOUT,
        1,
    )
    await tick(core)

    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    [payment] = await payments(core)
    assert (payment.status, payment.outcome, payment.failure_reason) == (
        PaymentStatus.FAILED,
        PaymentOutcome.RECORDED,
        "expired",
    )
    [row] = await checkout_rows(core)
    assert (row.next_check_at, row.result) == (None, "recorded")
    # The member simply asks for a new invoice.
    _, second = await checkout(core)
    assert second != invoice_id


async def test_lookups_of_ids_crypto_pay_cannot_have_ask_nothing(
    api: FakeCryptoPay, cryptopay: CryptoPayProvider
) -> None:
    assert await cryptopay.check_payment("no_method:paidclub-1-2-0") is None
    assert api.requests == []
    assert await cryptopay.check_payment("123456") is None  # unknown to Crypto Pay
    foreign = api.add_foreign_invoice()
    with pytest.raises(CryptoPayError, match="lacks our payload"):
        await cryptopay.check_payment(foreign)


@pytest.mark.parametrize("amount", ["10", "10.00", Decimal("10.0"), 10])
def test_invoice_amounts_become_cents_without_floats(amount: object) -> None:
    invoice = {
        "invoice_id": 7001,
        "status": "paid",
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": amount,
        "payload": "paidclub:3:2",
        "created_at": "2026-02-28T09:00:00.000Z",
        "paid_at": "2026-02-28T09:05:12.345Z",
    }
    event = invoice_event(invoice)
    assert isinstance(event, PaymentSucceeded)
    assert (event.subscription_id, event.for_period, event.amount) == (3, 2, Money(1000, "USD"))
    assert event.occurred_at == datetime(2026, 2, 28, 9, 5, 12, 345000, tzinfo=UTC)


# --- renewals: a new invoice every period ------------------------------------------------------


async def test_a_renewal_has_nothing_to_charge_and_asks_crypto_pay_nothing(
    cryptopay: CryptoPayProvider, api: FakeCryptoPay
) -> None:
    key = renewal_idempotence_key(7, 2, 0)
    request = RenewalRequest(
        subscription_id=7,
        member_telegram_id=MEMBER,
        plan_code="month",
        amount=Money(1000, "USD"),
        for_period=2,
        attempt=0,
        payment_method_id=None,
        idempotence_key=key,
        description="Продление подписки «1 месяц»",
    )
    result = await cryptopay.charge_renewal(request)
    assert isinstance(result, PaymentFailed)
    assert (result.reason, result.provider_payment_id) == (
        "no_saved_payment_method",
        f"no_method:{key}",
    )
    assert api.requests == []


async def test_crypto_members_are_reminded_to_pay_and_told_when_the_period_ended_unpaid(
    core: Core,
    api: FakeCryptoPay,
    cryptopay: CryptoPayProvider,
    http: httpx.AsyncClient,
    clock: FakeClock,
) -> None:
    sub_id, _ = await paid_subscription(core, api, http)
    pay = [{"text": "Оплатить 10 $", "callback_data": f"pay:{sub_id}"}]
    for moment in (FEB_28 - 3 * DAY, FEB_28 - DAY):
        clock.set(moment)
        assert (await tick(core)).reminders == 1
    early, late = await notifications(core, "reminder")
    assert early.text.startswith(
        "Через 3 дня, 28.02.2026, заканчивается оплаченный период. При оплате криптовалютой "
        "подписка сама не продлевается"
    )
    assert late.text.startswith("Завтра, 28.02.2026, заканчивается оплаченный период.")
    assert early.buttons == late.buttons == pay

    # The period end and the first dunning step each repeat the "pay" button; no charge is
    # promised, because there is nothing to charge.
    for moment in (FEB_28, FEB_28 + DAY):
        clock.set(moment)
        await tick(core)
    unpaid = await notifications(core, "payment_failed")
    assert len(unpaid) == 2
    for message in unpaid:
        assert message.text.startswith("Оплаченный период закончился, а следующий не оплачен")
        assert "Доступ к клубу сохранится до 03.03.2026." in message.text
        assert message.buttons == pay
    assert (await load_sub(core, sub_id)).status is SubStatus.PAST_DUE


async def test_the_next_period_can_be_paid_early_and_starts_where_the_last_one_ends(
    core: Core,
    api: FakeCryptoPay,
    cryptopay: CryptoPayProvider,
    http: httpx.AsyncClient,
    clock: FakeClock,
) -> None:
    sub_id, _ = await paid_subscription(core, api, http)
    clock.set(FEB_28 - 3 * DAY)

    invoice_id = await repay(core, sub_id)
    assert json.loads(api.requests[-1].content)["payload"] == f"paidclub:{sub_id}:2"
    api.pay(invoice_id)
    assert await deliver(api, http, invoice_id) == [
        {"outcome": "applied", "subscription_id": sub_id}
    ]

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid) == (SubStatus.ACTIVE, 2)
    assert (sub.current_period_start, sub.current_period_end) == (FEB_28, MAR_31)
    assert sub.next_charge_at == MAR_31
    [renewed] = await notifications(core, "renewed")
    assert "продлена до 31.03.2026. Оплачено: 10 $." in renewed.text
    clock.set(FEB_28)
    assert (await tick(core)).charged == 0


async def test_after_the_period_end_a_new_invoice_paid_during_grace_renews_from_the_old_end(
    core: Core,
    api: FakeCryptoPay,
    cryptopay: CryptoPayProvider,
    http: httpx.AsyncClient,
    clock: FakeClock,
) -> None:
    sub_id, _ = await paid_subscription(core, api, http)
    requests_before = len(api.requests)

    clock.set(FEB_28)
    report = await tick(core)

    assert (report.charged, report.charge_failed) == (1, 1)
    assert len(api.requests) == requests_before  # nothing was asked of Crypto Pay
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.PAST_DUE
    assert sub.grace_until == FEB_28 + 3 * DAY
    failure = (await payments(core))[-1]
    assert (failure.failure_reason, failure.for_period) == ("no_saved_payment_method", 2)
    [failed] = await notifications(core, "payment_failed")
    assert failed.buttons == [{"text": "Оплатить 10 $", "callback_data": f"pay:{sub_id}"}]

    clock.set(FEB_28 + DAY - timedelta(hours=1))
    invoice_id = await repay(core, sub_id)
    api.pay(invoice_id)
    await deliver(api, http, invoice_id)

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid) == (SubStatus.ACTIVE, 2)
    assert (sub.current_period_start, sub.current_period_end) == (FEB_28, MAR_31)
    assert (sub.grace_until, sub.dunning_attempt) == (None, 0)
    async with core.sessions() as session:
        [attempt] = (await session.scalars(select(ChargeAttempt))).all()
    assert attempt.provider_payment_id == f"no_method:{renewal_idempotence_key(sub_id, 2, 0)}"


async def test_after_grace_a_new_invoice_starts_a_new_period_from_the_payment(
    core: Core,
    api: FakeCryptoPay,
    cryptopay: CryptoPayProvider,
    http: httpx.AsyncClient,
    clock: FakeClock,
) -> None:
    sub_id, _ = await paid_subscription(core, api, http)
    for moment in (FEB_28, FEB_28 + DAY, FEB_28 + 3 * DAY):
        clock.set(moment)
        await tick(core)
    assert (await load_sub(core, sub_id)).status is SubStatus.EXPIRED

    back = FEB_28 + 5 * DAY
    clock.set(back)
    invoice_id = await repay(core, sub_id)
    api.pay(invoice_id)
    await deliver(api, http, invoice_id)

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid) == (SubStatus.ACTIVE, 2)
    assert (sub.current_period_start, sub.current_period_end) == (back, back + 31 * DAY)
    assert "снова открыт" in (await notifications(core, "activated"))[-1].text


async def test_a_second_invoice_paid_for_the_same_period_is_flagged_for_a_refund(
    core: Core,
    api: FakeCryptoPay,
    cryptopay: CryptoPayProvider,
    http: httpx.AsyncClient,
    clock: FakeClock,
) -> None:
    sub_id, _ = await paid_subscription(core, api, http)
    clock.set(FEB_28 - DAY)
    first, second = await repay(core, sub_id), await repay(core, sub_id)
    api.pay(first)
    api.pay(second)

    assert await deliver(api, http, first) == [{"outcome": "applied", "subscription_id": sub_id}]
    assert await deliver(api, http, second) == [
        {"outcome": "needs_refund", "subscription_id": sub_id}
    ]
    assert (await load_sub(core, sub_id)).current_period_end == MAR_31
    [alert] = await notifications(core, "owner_alert")
    assert f"cryptopay {second}" in alert.text


def test_the_signature_is_an_hmac_of_the_raw_body_keyed_with_the_token_hash() -> None:
    """The scheme Crypto Pay documents, spelled out independently of the provider's code."""
    body = b'{"update_id":1,"update_type":"invoice_paid"}'
    key = hashlib.sha256(TOKEN.encode()).digest()
    assert signature(TOKEN, body) == hmac.new(key, body, hashlib.sha256).hexdigest()
    # Not keyed with the token itself, and not over a re-serialised body.
    assert signature(TOKEN, body) != hmac.new(TOKEN.encode(), body, hashlib.sha256).hexdigest()
    assert signature(TOKEN, body) != signature(TOKEN, json.dumps(json.loads(body)).encode())
