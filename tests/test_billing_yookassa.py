from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from paidclub import club
from paidclub.access import decide_join_request
from paidclub.billing import ProviderRegistry
from paidclub.billing.base import (
    PaymentFailed,
    PaymentPending,
    PaymentPurpose,
    PaymentRefunded,
    PaymentSucceeded,
    ProviderError,
    RenewalRequest,
    WebhookRejected,
)
from paidclub.billing.yookassa import YooKassaError, YooKassaProvider
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.domain.subscriptions import ExpireReason, SubStatus, renewal_idempotence_key
from paidclub.models import PaymentOutcome
from paidclub.money import Money
from paidclub.payments import apply_event
from paidclub.scheduler import Scheduler
from paidclub.web.payments import create_webhook_router, process_webhook
from tests.conftest import START
from tests.helpers import CHAT_ID, FakeRevoker, load_sub, notifications, payments

SHOP_ID = "123456"
SECRET = "test_secret_key"
MEMBER = 801
FEB_28 = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
MAR_31 = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeYooKassa:
    """An in-memory YooKassa API behind ``httpx.MockTransport``.

    It keeps the behaviour the provider relies on: Basic auth, a required ``Idempotence-Key``
    whose repeat returns the first answer unchanged (for :attr:`key_ttl`, 24 hours as at
    YooKassa), payments that move from pending to succeeded or canceled, 404 for unknown
    payments, and ``GET /payments`` with ``created_at.gte``, ``limit`` and ``cursor``, newest
    first. ``script`` queues raw responses or transport errors for the next requests.
    """

    key_ttl = timedelta(hours=24)

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.payments: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.answers: dict[str, dict[str, Any]] = {}
        self.key_created: dict[str, datetime] = {}
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response | Exception] = []
        #: Status a merchant-initiated (saved method) payment gets when created.
        self.renewal_status = "succeeded"
        #: The next this many renewal POSTs create their payment, but the answer is lost.
        self.lost_renewal_answers = 0
        #: When set, every renewal POST gets this answer and creates nothing.
        self.refuse_renewals: httpx.Response | None = None
        self.decline_reason = "insufficient_funds"

    # --- test controls ---------------------------------------------------------------------

    def pay(self, payment_id: str, *, save: bool = True) -> None:
        payment = self.payments[payment_id]
        payment["status"] = "succeeded"
        payment["paid"] = True
        payment["captured_at"] = _iso(self.clock.now())
        payment["payment_method"] = {
            "type": "bank_card",
            "id": f"pm-{payment_id}",
            "saved": save,
            "title": "Bank card *4477",
        }

    def decline(self, payment_id: str, reason: str) -> None:
        payment = self.payments[payment_id]
        payment["status"] = "canceled"
        payment["cancellation_details"] = {"party": "payment_network", "reason": reason}

    def refund(self, payment_id: str, value: str | None = None) -> str:
        """The owner refunds (part of) a payment in the shop's dashboard."""
        payment = self.payments[payment_id]
        refund_id = f"2f{len(self.refunds) + 1:06x}-0015-5000-9000-{len(self.refunds) + 1:012x}"
        self.refunds[refund_id] = {
            "id": refund_id,
            "payment_id": payment_id,
            "status": "succeeded",
            "created_at": _iso(self.clock.now()),
            "amount": {"value": value or payment["amount"]["value"], "currency": "RUB"},
        }
        return refund_id

    def refund_notification(self, refund_id: str) -> bytes:
        stored = self.refunds[refund_id]
        return json.dumps(
            {"type": "notification", "event": "refund.succeeded", "object": dict(stored)}
        ).encode()

    def add_foreign_payment(self) -> str:
        """A payment of the same shop that this app did not create (no marker)."""
        payment_id = self._new_id()
        self.payments[payment_id] = {
            "id": payment_id,
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "100.00", "currency": "RUB"},
            "created_at": _iso(self.clock.now()),
            "metadata": {"order": "42"},
        }
        return payment_id

    def notification(self, payment_id: str, event: str = "payment.succeeded") -> bytes:
        stored = self.payments.get(payment_id, {"id": payment_id})
        return json.dumps({"type": "notification", "event": event, "object": dict(stored)}).encode()

    def posts(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]

    # --- the API ---------------------------------------------------------------------------

    def _new_id(self) -> str:
        n = len(self.payments) + 1
        return f"2f{n:06x}-000f-5000-8000-{n:012x}"

    @staticmethod
    def _error(status: int, code: str, description: str, **extra: str) -> httpx.Response:
        body = {"type": "error", "id": "err-1", "code": code, "description": description}
        return httpx.Response(status, json={**body, **extra})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            scripted = self.script.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        expected = "Basic " + base64.b64encode(f"{SHOP_ID}:{SECRET}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return self._error(401, "invalid_credentials", "Login or password is incorrect")
        path = request.url.path
        renewal = request.method == "POST" and b"payment_method_id" in request.content
        if renewal and self.refuse_renewals is not None:
            return self.refuse_renewals
        if renewal and self.lost_renewal_answers:
            self.lost_renewal_answers -= 1
            self._create(request)
            raise httpx.ReadTimeout("answer lost after the payment was created", request=request)
        if request.method == "POST" and path == "/v3/payments":
            return self._create(request)
        if request.method == "GET" and path == "/v3/payments":
            return self._list(request)
        if request.method == "GET" and path.startswith("/v3/refunds/"):
            refund = self.refunds.get(path.rsplit("/", 1)[1])
            if refund is None:
                return self._error(404, "not_found", "Refund not found")
            return httpx.Response(200, json=refund)
        if request.method == "GET" and path.startswith("/v3/payments/"):
            payment = self.payments.get(path.rsplit("/", 1)[1])
            if payment is None:
                return self._error(404, "not_found", "Payment not found")
            return httpx.Response(200, json=payment)
        return httpx.Response(405)

    def _create(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotence-Key")
        if not key:
            return self._error(
                400, "invalid_request", "Idempotence key missing", parameter="Idempotence-Key"
            )
        created = self.key_created.get(key)
        if created is not None and self.clock.now() - created >= self.key_ttl:
            del self.answers[key]  # YooKassa has forgotten the key: this is a new payment
        if key in self.answers:
            return httpx.Response(200, json=self.answers[key])
        self.key_created[key] = self.clock.now()
        body = json.loads(request.content)
        payment_id = self._new_id()
        payment: dict[str, Any] = {
            "id": payment_id,
            "status": "pending",
            "paid": False,
            "amount": body["amount"],
            "description": body.get("description"),
            "metadata": body.get("metadata", {}),
            "created_at": _iso(self.clock.now()),
            "test": True,
        }
        if "payment_method_id" in body:
            payment["payment_method"] = {
                "type": "bank_card",
                "id": body["payment_method_id"],
                "saved": True,
            }
            if self.renewal_status == "succeeded":
                payment.update(status="succeeded", paid=True, captured_at=payment["created_at"])
            elif self.renewal_status == "canceled":
                payment["status"] = "canceled"
                payment["cancellation_details"] = {
                    "party": "payment_network",
                    "reason": self.decline_reason,
                }
        else:
            payment["confirmation"] = {
                "type": "redirect",
                "return_url": body["confirmation"]["return_url"],
                "confirmation_url": "https://yoomoney.ru/checkout/payments/v2/contract"
                f"?orderId={payment_id}",
            }
        self.payments[payment_id] = payment
        self.answers[key] = json.loads(json.dumps(payment))
        return httpx.Response(200, json=payment)

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        since = params.get("created_at.gte")
        limit = int(params.get("limit", "10"))
        start = int(params.get("cursor", "0"))
        newest_first = sorted(
            self.payments.values(), key=lambda payment: payment["created_at"], reverse=True
        )
        if since is not None:
            newest_first = [p for p in newest_first if p["created_at"] >= since]
        page = newest_first[start : start + limit]
        body: dict[str, Any] = {"type": "list", "items": page}
        if start + limit < len(newest_first):
            body["next_cursor"] = str(start + limit)
        return httpx.Response(200, json=body)


@pytest.fixture
def api(clock: FakeClock) -> FakeYooKassa:
    return FakeYooKassa(clock)


@pytest.fixture
async def yookassa(
    core: Core, api: FakeYooKassa, clock: FakeClock
) -> AsyncIterator[YooKassaProvider]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    provider = YooKassaProvider(
        shop_id=SHOP_ID,
        secret_key=SECRET,
        return_url=core.settings.public_base_url,
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


async def checkout(core: Core, api: FakeYooKassa, plan: str = "month") -> tuple[int, str]:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code=plan, provider="yookassa")
    payment_id = link.url.rsplit("=", 1)[1]
    assert payment_id in api.payments
    return link.subscription_id, payment_id


async def paid_subscription(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, *, save: bool = True
) -> tuple[int, str]:
    sub_id, payment_id = await checkout(core, api)
    api.pay(payment_id, save=save)
    [event] = await yookassa.parse_webhook({}, api.notification(payment_id))
    assert (await apply_event(core, event)).outcome is PaymentOutcome.APPLIED
    return sub_id, payment_id


def renewal_request(**changes: Any) -> RenewalRequest:
    values: dict[str, Any] = {
        "subscription_id": 7,
        "member_telegram_id": MEMBER,
        "plan_code": "month",
        "amount": Money(99000, "RUB"),
        "for_period": 2,
        "attempt": 0,
        "payment_method_id": "pm-saved",
        "idempotence_key": renewal_idempotence_key(7, 2, 0),
        "description": "Продление подписки «1 месяц»",
    }
    values.update(changes)
    return RenewalRequest(**values)


# --- checkout ----------------------------------------------------------------------------------


async def test_checkout_creates_a_one_stage_payment_that_saves_the_card(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    link = await club.start_checkout(
        core, telegram_id=MEMBER, plan_code="month", provider="yookassa"
    )

    [request] = api.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.yookassa.ru/v3/payments"
    assert len(request.headers["Idempotence-Key"]) <= 64
    body = json.loads(request.content)
    assert body == {
        "amount": {"value": "990.00", "currency": "RUB"},
        "capture": True,
        "save_payment_method": True,
        "confirmation": {"type": "redirect", "return_url": "http://localhost:8000"},
        "description": "Подписка на клуб: 1 месяц",
        "metadata": {
            "paidclub": "1",
            "subscription_id": str(link.subscription_id),
            "telegram_id": str(MEMBER),
            "for_period": "1",
            "purpose": "checkout",
        },
    }
    [payment_id] = api.payments
    assert link.url == api.payments[payment_id]["confirmation"]["confirmation_url"]
    assert link.amount == Money(99000, "RUB")


async def test_amounts_are_decimal_strings_and_the_return_url_comes_from_settings(
    tmp_path: Path, api: FakeYooKassa, clock: FakeClock
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'yookassa.db'}",
        providers=["yookassa"],
        yookassa_shop_id=SHOP_ID,
        yookassa_secret_key=SECRET,  # type: ignore[arg-type]
        checkout_return_url="https://t.me/club_demo_bot",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        provider = YooKassaProvider.from_settings(settings, client=client, clock=clock)
        core = await build_core(settings, clock=clock, providers=ProviderRegistry([provider]))
        try:
            await club.start_checkout(
                core, telegram_id=MEMBER, plan_code="quarter", provider="yookassa"
            )
        finally:
            await core.aclose()
    body = json.loads(api.requests[0].content)
    assert body["amount"] == {"value": "2490.00", "currency": "RUB"}
    assert body["confirmation"]["return_url"] == "https://t.me/club_demo_bot"


def test_from_settings_needs_shop_credentials(settings: Settings) -> None:
    with pytest.raises(ValueError, match="YOOKASSA_SHOP_ID"):
        YooKassaProvider.from_settings(settings)


async def test_checkout_is_retried_with_the_same_key_after_a_server_error(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    api.script.append(httpx.Response(500, json={"type": "error", "code": "internal_server_error"}))
    sub_id, _ = await checkout(core, api)

    first, second = api.posts()
    assert first.headers["Idempotence-Key"] == second.headers["Idempotence-Key"]
    assert len(api.payments) == 1
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING


async def test_a_rejected_checkout_request_is_not_retried(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    api.script.append(
        FakeYooKassa._error(400, "invalid_request", "Receipt is missing", parameter="receipt")
    )
    with pytest.raises(YooKassaError) as caught:
        await checkout(core, api)
    assert (caught.value.status, caught.value.code, caught.value.parameter) == (
        400,
        "invalid_request",
        "receipt",
    )
    assert "Receipt is missing" in str(caught.value)
    assert len(api.posts()) == 1


# --- webhooks ----------------------------------------------------------------------------------


async def test_webhook_applies_the_payment_as_fetched_from_the_api(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, http: httpx.AsyncClient
) -> None:
    sub_id, payment_id = await checkout(core, api)
    api.pay(payment_id)

    response = await http.post("/webhooks/yookassa", content=api.notification(payment_id))

    assert response.status_code == 200
    assert response.json()["results"] == [{"outcome": "applied", "subscription_id": sub_id}]
    [lookup] = [request for request in api.requests if request.method == "GET"]
    assert lookup.url.path == f"/v3/payments/{payment_id}"
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.current_period_end == FEB_28
    assert sub.payment_method_id == f"pm-{payment_id}"
    [payment] = await payments(core)
    assert (payment.provider, payment.provider_payment_id) == ("yookassa", payment_id)
    assert payment.amount_minor == 99000


async def test_the_webhook_body_is_not_trusted(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, http: httpx.AsyncClient
) -> None:
    sub_id, payment_id = await checkout(core, api)
    forged = {
        "type": "notification",
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "amount": {"value": "990.00", "currency": "RUB"},
            "payment_method": {"id": "pm-attacker", "saved": True},
            "metadata": {"paidclub": "1", "subscription_id": str(sub_id), "purpose": "checkout"},
            "created_at": _iso(START),
        },
    }

    response = await http.post("/webhooks/yookassa", json=forged)

    # YooKassa says the payment is still pending, so nothing is applied.
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    assert await payments(core) == []


async def test_a_repeated_webhook_pays_the_period_once(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, http: httpx.AsyncClient
) -> None:
    sub_id, payment_id = await paid_subscription(core, api, yookassa)

    response = await http.post("/webhooks/yookassa", content=api.notification(payment_id))

    assert response.json()["results"] == [{"outcome": "duplicate", "subscription_id": sub_id}]
    sub = await load_sub(core, sub_id)
    assert (sub.periods_paid, sub.current_period_end) == (1, FEB_28)
    assert len(await payments(core)) == 1


async def test_a_card_that_was_not_saved_leaves_no_payment_method(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    sub_id, _ = await paid_subscription(core, api, yookassa, save=False)
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.payment_method_id is None


async def test_an_abandoned_checkout_is_recorded_without_touching_the_subscription(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    sub_id, payment_id = await checkout(core, api)
    api.decline(payment_id, "expired_on_confirmation")

    [event] = await yookassa.parse_webhook({}, api.notification(payment_id, "payment.canceled"))

    assert isinstance(event, PaymentFailed)
    assert (event.reason, event.purpose) == ("expired_on_confirmation", PaymentPurpose.CHECKOUT)
    assert (await apply_event(core, event)).outcome is PaymentOutcome.RECORDED
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING


async def test_a_notification_for_an_unknown_payment_is_rejected(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, http: httpx.AsyncClient
) -> None:
    body = api.notification("2fffffff-000f-5000-8000-000000000999")
    with pytest.raises(WebhookRejected):
        await yookassa.parse_webhook({}, body)
    assert (await http.post("/webhooks/yookassa", content=body)).status_code == 400


async def test_the_webhook_asks_for_a_retry_while_the_api_cannot_confirm(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, http: httpx.AsyncClient
) -> None:
    sub_id, payment_id = await checkout(core, api)
    api.pay(payment_id)
    api.script.append(httpx.ConnectError("connection refused"))

    response = await http.post("/webhooks/yookassa", content=api.notification(payment_id))

    assert response.status_code == 502
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    # YooKassa delivers it again; this time the lookup works.
    response = await http.post("/webhooks/yookassa", content=api.notification(payment_id))
    assert response.status_code == 200
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE


async def test_foreign_payments_and_other_events_are_ignored(
    api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    foreign = api.add_foreign_payment()
    assert await yookassa.parse_webhook({}, api.notification(foreign)) == []

    method = json.dumps(
        {"type": "notification", "event": "payment_method.active", "object": {"id": "pm-1"}}
    ).encode()
    before = len(api.requests)
    assert await yookassa.parse_webhook({}, method) == []
    assert len(api.requests) == before


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"type": "something", "event": "payment.succeeded", "object": {"id": "x"}}',
        b'{"type": "notification", "event": "payment.succeeded"}',
        b'{"type": "notification", "event": "payment.succeeded", "object": {}}',
        b'{"type": "notification", "event": "payment.succeeded", "object": {"id": "../me"}}',
    ],
)
async def test_malformed_notifications_are_rejected_without_api_calls(
    api: FakeYooKassa, yookassa: YooKassaProvider, body: bytes
) -> None:
    with pytest.raises(WebhookRejected):
        await yookassa.parse_webhook({}, body)
    assert api.requests == []


# --- renewals ----------------------------------------------------------------------------------


async def test_a_renewal_charges_the_saved_method_with_the_scheduler_key(
    api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    result = await yookassa.charge_renewal(renewal_request())

    assert isinstance(result, PaymentSucceeded)
    assert (result.subscription_id, result.for_period, result.attempt) == (7, 2, 0)
    assert result.purpose is PaymentPurpose.RENEWAL
    assert result.amount == Money(99000, "RUB")
    [request] = api.requests
    assert request.headers["Idempotence-Key"] == "paidclub-7-2-0"
    body = json.loads(request.content)
    assert body["payment_method_id"] == "pm-saved"
    assert body["capture"] is True
    assert "confirmation" not in body
    assert "save_payment_method" not in body
    assert body["metadata"]["attempt"] == "0"
    assert body["metadata"]["purpose"] == "renewal"


async def test_a_declined_renewal_reports_the_bank_reason(
    api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    api.renewal_status = "canceled"
    api.decline_reason = "permission_revoked"

    result = await yookassa.charge_renewal(renewal_request(attempt=1))

    assert isinstance(result, PaymentFailed)
    assert (result.reason, result.for_period, result.attempt) == ("permission_revoked", 2, 1)


async def test_a_pending_renewal_is_rechecked_with_the_same_key(
    api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    api.renewal_status = "pending"
    first = await yookassa.charge_renewal(renewal_request())
    assert isinstance(first, PaymentPending)

    api.pay(first.provider_payment_id)
    again = await yookassa.charge_renewal(renewal_request())

    assert isinstance(again, PaymentSucceeded)
    assert again.provider_payment_id == first.provider_payment_id
    assert len(api.payments) == 1
    keys = {request.headers["Idempotence-Key"] for request in api.posts()}
    assert keys == {"paidclub-7-2-0"}


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"type": "error", "code": "internal_server_error"}),
        httpx.Response(429, json={"type": "error", "code": "too_many_requests"}),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
    ],
)
async def test_an_unknown_renewal_outcome_raises_provider_error(
    api: FakeYooKassa, yookassa: YooKassaProvider, failure: httpx.Response | Exception
) -> None:
    api.script.append(failure)
    with pytest.raises(ProviderError):
        await yookassa.charge_renewal(renewal_request())


async def test_a_settled_payment_without_our_metadata_is_not_mistaken_for_pending(
    api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    api.script.append(
        httpx.Response(
            200,
            json={
                "id": "2f000009-000f-5000-8000-000000000009",
                "status": "succeeded",
                "amount": {"value": "990.00", "currency": "RUB"},
                "created_at": _iso(START),
                "metadata": {},
            },
        )
    )
    with pytest.raises(YooKassaError, match="metadata"):
        await yookassa.charge_renewal(renewal_request())


async def test_a_renewal_without_a_saved_method_fails_without_calling_yookassa(
    api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    result = await yookassa.charge_renewal(renewal_request(payment_method_id=None))
    assert isinstance(result, PaymentFailed)
    assert result.reason == "no_saved_payment_method"
    assert api.requests == []


async def test_a_renewal_refused_for_wrong_credentials_is_a_failed_charge_for_the_owner(
    core: Core, api: FakeYooKassa, clock: FakeClock
) -> None:
    """Only a 500 leaves the result unknown; a 401 created no payment."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        provider = YooKassaProvider(
            shop_id=SHOP_ID, secret_key="wrong", return_url="http://x", client=client, clock=clock
        )
        result = await provider.charge_renewal(renewal_request())
        with pytest.raises(YooKassaError) as caught:
            await provider.check_payment("2f000001-000f-5000-8000-000000000001")
    assert isinstance(result, PaymentFailed)
    assert result.provider_payment_id == f"refused:{renewal_idempotence_key(7, 2, 0)}"
    assert result.reason == "invalid_credentials"
    assert result.owner_note is not None
    assert "PAIDCLUB_YOOKASSA_SECRET_KEY" in result.owner_note
    assert caught.value.status == 401


@pytest.mark.parametrize(
    ("status", "code", "owner_told"),
    [(400, "invalid_request", False), (403, "forbidden", True), (404, "not_found", False)],
)
async def test_final_refusals_of_a_renewal_are_failed_charges(
    api: FakeYooKassa, yookassa: YooKassaProvider, status: int, code: str, owner_told: bool
) -> None:
    api.script.append(FakeYooKassa._error(status, code, "refused"))
    result = await yookassa.charge_renewal(renewal_request())
    assert isinstance(result, PaymentFailed)
    assert result.reason == code
    assert (result.owner_note is not None) is owner_told


@pytest.mark.parametrize("status", [429, 500, 502])
async def test_answers_that_leave_the_outcome_unknown_raise(
    api: FakeYooKassa, yookassa: YooKassaProvider, status: int
) -> None:
    api.script.append(FakeYooKassa._error(status, "internal_server_error", "try later"))
    with pytest.raises(YooKassaError) as caught:
        await yookassa.charge_renewal(renewal_request())
    assert caught.value.status == status


async def test_the_scheduler_renews_through_yookassa_from_the_period_end(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    sub_id, first_payment = await paid_subscription(core, api, yookassa)
    clock.set(FEB_28)

    report = await Scheduler(core, FakeRevoker()).tick()

    assert (report.charged, report.renewed, report.errors) == (1, 1, 0)
    renewal = api.posts()[-1]
    assert renewal.headers["Idempotence-Key"] == renewal_idempotence_key(sub_id, 2, 0)
    assert json.loads(renewal.content)["payment_method_id"] == f"pm-{first_payment}"
    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid) == (SubStatus.ACTIVE, 2)
    assert (sub.current_period_start, sub.current_period_end) == (FEB_28, MAR_31)


async def test_a_pending_renewal_settled_by_webhook_is_not_charged_again(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    sub_id, _ = await paid_subscription(core, api, yookassa)
    clock.set(FEB_28)
    api.renewal_status = "pending"

    report = await Scheduler(core, FakeRevoker()).tick()
    assert (report.charged, report.pending) == (1, 1)

    renewal_id = list(api.payments)[-1]
    api.pay(renewal_id)
    [event] = await yookassa.parse_webhook({}, api.notification(renewal_id))
    assert isinstance(event, PaymentSucceeded)
    assert (event.purpose, event.for_period, event.attempt) == (PaymentPurpose.RENEWAL, 2, 0)
    assert (await apply_event(core, event)).outcome is PaymentOutcome.APPLIED

    clock.set(FEB_28.replace(hour=10))
    posts_before = len(api.posts())
    report = await Scheduler(core, FakeRevoker()).tick()
    assert report.charged == 0
    assert len(api.posts()) == posts_before
    assert (await load_sub(core, sub_id)).current_period_end == MAR_31


# --- renewals whose answer never came, refusals ----------------------------------------------


def renewal_payments(api: FakeYooKassa, sub_id: int, period: int) -> list[dict[str, Any]]:
    return [
        payment
        for payment in api.payments.values()
        if payment["metadata"].get("subscription_id") == str(sub_id)
        and payment["metadata"].get("for_period") == str(period)
    ]


async def test_a_renewal_whose_answer_was_lost_is_looked_up_once_the_key_may_be_forgotten(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    """The POST reached YooKassa and the card was charged, but the answer was lost; the
    process was then down for more than a day, so the notification was lost too. Sending the
    same key again would be a new payment: the payment is found in the list instead."""
    sub_id, _ = await paid_subscription(core, api, yookassa)
    api.lost_renewal_answers = 1
    clock.set(FEB_28)
    assert (await Scheduler(core, FakeRevoker()).tick()).provider_errors == 1

    clock.advance(timedelta(hours=25))
    report = await Scheduler(core, FakeRevoker()).tick()

    assert report.renewed == 1
    assert [p["status"] for p in renewal_payments(api, sub_id, 2)] == ["succeeded"]
    assert len([r for r in api.posts() if b"payment_method_id" in r.content]) == 1
    [search] = [r for r in api.requests if r.method == "GET" and r.url.path == "/v3/payments"]
    assert search.url.params["created_at.gte"] == "2026-02-28T08:50:00.000Z"
    sub = await load_sub(core, sub_id)
    assert (sub.periods_paid, sub.current_period_end) == (2, MAR_31)


async def test_a_renewal_that_never_reached_yookassa_is_sent_again_after_a_day(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    sub_id, _ = await paid_subscription(core, api, yookassa)
    clock.set(FEB_28)
    api.script.append(httpx.ConnectError("connection refused"))
    assert (await Scheduler(core, FakeRevoker()).tick()).provider_errors == 1

    clock.advance(timedelta(hours=25))
    assert (await Scheduler(core, FakeRevoker()).tick()).renewed == 1
    assert [p["status"] for p in renewal_payments(api, sub_id, 2)] == ["succeeded"]


async def test_a_search_that_cannot_finish_does_not_charge(
    api: FakeYooKassa,
    yookassa: YooKassaProvider,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("paidclub.billing.yookassa.SEARCH_MAX_PAGES", 2)
    for _ in range(250):
        api.add_foreign_payment()
    clock.set(START + timedelta(hours=30))

    with pytest.raises(YooKassaError, match="cannot tell"):
        await yookassa.charge_renewal(renewal_request(first_sent_at=START))
    assert api.posts() == []


async def test_a_pending_renewal_is_checked_with_get_and_applied_when_decided(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    sub_id, _ = await paid_subscription(core, api, yookassa)
    api.renewal_status = "pending"
    clock.set(FEB_28)
    assert (await Scheduler(core, FakeRevoker()).tick()).pending == 1
    [renewal_id] = [p["id"] for p in renewal_payments(api, sub_id, 2)]
    posts = len(api.posts())

    clock.advance(timedelta(seconds=900))
    assert (await Scheduler(core, FakeRevoker()).tick()).pending_checked == 1
    api.pay(renewal_id)
    clock.advance(timedelta(seconds=900))
    assert (await Scheduler(core, FakeRevoker()).tick()).renewed == 1

    assert len(api.posts()) == posts
    gets = [r.url.path for r in api.requests if r.method == "GET"]
    assert gets[-2:] == [f"/v3/payments/{renewal_id}"] * 2
    assert (await load_sub(core, sub_id)).current_period_end == MAR_31


async def test_renewals_yookassa_refuses_for_good_go_through_dunning_and_end_access(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    """Autopayments switched off for the shop: every renewal POST gets 403 forbidden."""
    sub_id, _ = await paid_subscription(core, api, yookassa)
    api.refuse_renewals = FakeYooKassa._error(
        403, "forbidden", "Не хватает прав для совершения операции"
    )
    clock.set(FEB_28)
    for _ in range(10):
        await Scheduler(core, FakeRevoker()).tick()
        clock.advance(timedelta(days=1))

    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.EXPIRED
    assert len([r for r in api.posts() if b"payment_method_id" in r.content]) == 3
    failures = [p for p in await payments(core) if p.purpose is PaymentPurpose.RENEWAL]
    assert {p.failure_reason for p in failures} == {"forbidden"}
    decision = await decide_join_request(core, chat_id=CHAT_ID, telegram_id=MEMBER)
    assert decision.action == "decline"
    alerts = await notifications(core, "owner_alert")
    assert len(alerts) == 3  # at most once a day: Feb 28, Mar 1 and Mar 3
    assert all("менеджер ЮKassa" in alert.text for alert in alerts)


# --- refunds ---------------------------------------------------------------------------------


async def test_a_refund_turns_auto_renew_off_and_forgets_the_card(
    core: Core,
    api: FakeYooKassa,
    yookassa: YooKassaProvider,
    clock: FakeClock,
    http: httpx.AsyncClient,
) -> None:
    sub_id, payment_id = await paid_subscription(core, api, yookassa)
    refund_id = api.refund(payment_id)

    forged = json.dumps(
        {
            "type": "notification",
            "event": "refund.succeeded",
            "object": {"id": refund_id, "payment_id": "someone-else", "status": "succeeded"},
        }
    ).encode()
    response = await http.post("/webhooks/yookassa", content=forged)
    assert response.status_code == 200
    assert response.json()["results"] == [{"outcome": "applied", "subscription_id": sub_id}]
    # The refund was fetched with the shop credentials; the body's payment id is not trusted.
    assert api.requests[-1].url.path == f"/v3/refunds/{refund_id}"
    again = await http.post("/webhooks/yookassa", content=api.refund_notification(refund_id))
    assert again.json()["results"][0]["outcome"] == "duplicate"

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.payment_method_id, sub.next_charge_at) == (
        SubStatus.CANCELED,
        None,
        None,
    )
    [member] = await notifications(core, "refunded")
    assert "990 ₽" in member.text and "сохранится до 28.02.2026" in member.text
    [alert] = await notifications(core, "owner_alert")
    assert "отключено автопродление" in alert.text

    before = len(api.posts())
    clock.set(FEB_28)
    await Scheduler(core, FakeRevoker()).tick()
    assert len(api.posts()) == before
    assert (await load_sub(core, sub_id)).status is SubStatus.EXPIRED


async def test_a_refund_while_past_due_ends_access_and_stops_the_retries(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    sub_id, payment_id = await paid_subscription(core, api, yookassa)
    api.renewal_status = "canceled"
    clock.set(FEB_28)
    await Scheduler(core, FakeRevoker()).tick()
    assert (await load_sub(core, sub_id)).status is SubStatus.PAST_DUE

    refund_id = api.refund(payment_id, "500.00")  # a part of it
    [event] = await yookassa.parse_webhook({}, api.refund_notification(refund_id))
    assert isinstance(event, PaymentRefunded)
    assert (event.provider_payment_id, event.amount) == (payment_id, Money(50000, "RUB"))
    [result] = await process_webhook(core, "yookassa", {}, api.refund_notification(refund_id))
    assert result.outcome is PaymentOutcome.APPLIED

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.expire_reason) == (SubStatus.EXPIRED, ExpireReason.CANCELED)
    posts = len(api.posts())
    clock.set(FEB_28 + timedelta(days=1))
    await Scheduler(core, FakeRevoker()).tick()
    assert len(api.posts()) == posts


async def test_a_refund_of_a_payment_this_app_did_not_make_is_only_recorded(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider
) -> None:
    refund_id = api.refund(api.add_foreign_payment())
    [result] = await process_webhook(core, "yookassa", {}, api.refund_notification(refund_id))
    assert (result.outcome, result.subscription_id) == (PaymentOutcome.RECORDED, None)
    assert await notifications(core) == []
