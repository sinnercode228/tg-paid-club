from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from paidclub import club
from paidclub.billing import ProviderRegistry
from paidclub.billing.base import CheckoutRequest
from paidclub.billing.fake import FakeProvider
from paidclub.container import Core
from paidclub.db import ConcurrentUpdateError
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import PaymentOutcome, PaymentStatus
from paidclub.money import Money
from paidclub.web import payments as payments_route
from paidclub.web.fake_checkout import create_fake_checkout_router
from paidclub.web.payments import MAX_BODY_BYTES, create_webhook_router
from tests.helpers import load_sub, payments

MEMBER = 601


def make_app(core: Core) -> FastAPI:
    app = FastAPI()
    app.include_router(create_webhook_router(core))
    app.include_router(create_fake_checkout_router(core))
    return app


@pytest.fixture
async def http(core: Core) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=make_app(core))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def checkout_path(core: Core, plan: str = "month") -> tuple[int, str]:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code=plan, provider="fake")
    path = httpx.URL(link.url).path
    assert path.startswith("/fake-checkout/")
    return link.subscription_id, path


# --- fake checkout page ------------------------------------------------------------------------


async def test_the_checkout_page_shows_the_plan_and_the_choices(
    core: Core, http: httpx.AsyncClient
) -> None:
    _, path = await checkout_path(core)

    response = await http.get(path)

    assert response.status_code == 200
    page = response.text
    assert "1 месяц" in page
    assert "990 ₽" in page
    assert "деньги не списываются" in page
    for choice in ("card_ok", "card_decline", "no_card", "decline"):
        assert f'value="{choice}"' in page


async def test_paying_on_the_page_goes_through_the_webhook_path(
    core: Core, http: httpx.AsyncClient
) -> None:
    sub_id, path = await checkout_path(core)

    response = await http.post(path, data={"choice": "card_ok"})

    assert response.status_code == 303
    assert response.headers["location"] == path
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.payment_method_id is not None
    assert sub.payment_method_id.startswith("fake_pm_ok_")
    [payment] = await payments(core)
    assert payment.outcome is PaymentOutcome.APPLIED
    page = (await http.get(path)).text
    assert "Оплата прошла" in page
    assert 'name="choice"' not in page


async def test_submitting_twice_pays_the_period_once(core: Core, http: httpx.AsyncClient) -> None:
    sub_id, path = await checkout_path(core)

    await http.post(path, data={"choice": "card_ok"})
    await http.post(path, data={"choice": "card_ok"})

    assert len(await payments(core)) == 1
    assert (await load_sub(core, sub_id)).periods_paid == 1


async def test_a_declined_card_can_be_retried_on_the_same_page(
    core: Core, http: httpx.AsyncClient
) -> None:
    sub_id, path = await checkout_path(core)

    await http.post(path, data={"choice": "decline"})
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    [failed] = await payments(core)
    assert failed.status is PaymentStatus.FAILED
    page = (await http.get(path)).text
    assert "Банк отклонил платёж" in page
    assert 'name="choice"' in page

    await http.post(path, data={"choice": "no_card"})
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.payment_method_id is None


async def test_a_card_that_declines_renewals_is_saved_for_the_dunning_demo(
    core: Core, http: httpx.AsyncClient
) -> None:
    sub_id, path = await checkout_path(core)
    await http.post(path, data={"choice": "card_decline"})
    method = (await load_sub(core, sub_id)).payment_method_id
    assert method is not None
    assert method.startswith("fake_pm_decline_")


async def test_the_return_link_is_offered_after_paying(core: Core, http: httpx.AsyncClient) -> None:
    core.settings = core.settings.model_copy(
        update={"checkout_return_url": "https://t.me/club_demo_bot"}
    )
    _, path = await checkout_path(core)
    await http.post(path, data={"choice": "card_ok"})
    assert 'href="https://t.me/club_demo_bot"' in (await http.get(path)).text


@pytest.mark.parametrize("token", ["garbage", "eyJwaWQiOiJ4In0.deadbeef"])
async def test_forged_checkout_links_are_not_found(http: httpx.AsyncClient, token: str) -> None:
    assert (await http.get(f"/fake-checkout/{token}")).status_code == 404
    response = await http.post(f"/fake-checkout/{token}", data={"choice": "card_ok"})
    assert response.status_code == 404


async def test_an_unknown_choice_is_refused(core: Core, http: httpx.AsyncClient) -> None:
    sub_id, path = await checkout_path(core)
    response = await http.post(path, data={"choice": "free"})
    assert response.status_code == 400
    assert await payments(core) == []
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING


async def test_the_page_is_gone_without_the_fake_provider(core: Core, fake: FakeProvider) -> None:
    _, path = await checkout_path(core)
    core.providers = ProviderRegistry()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(core)), base_url="http://test"
    ) as client:
        assert (await client.get(path)).status_code == 404


async def test_the_page_escapes_owner_supplied_text(core: Core, http: httpx.AsyncClient) -> None:
    fake = core.providers.require("fake")
    assert isinstance(fake, FakeProvider)
    checkout = await fake.start_checkout(
        CheckoutRequest(
            subscription_id=1,
            member_telegram_id=MEMBER,
            plan_code="month",
            plan_title="<script>alert(1)</script>",
            amount=Money(99000, "RUB"),
            for_period=1,
            description="a & b",
        )
    )
    page = (await http.get(httpx.URL(checkout.url).path)).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "a &amp; b" in page


# --- webhook route -----------------------------------------------------------------------------


async def test_a_signed_webhook_is_applied_and_a_replay_is_a_duplicate(
    core: Core, fake: FakeProvider, http: httpx.AsyncClient
) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    headers, body = fake.checkout_webhook(session, succeeded=True)

    first = await http.post("/webhooks/fake", content=body, headers=headers)
    replay = await http.post("/webhooks/fake", content=body, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert first.json()["results"] == [
        {"outcome": "applied", "subscription_id": link.subscription_id}
    ]
    assert replay.json()["results"][0]["outcome"] == "duplicate"
    assert (await load_sub(core, link.subscription_id)).periods_paid == 1


async def test_an_unsigned_webhook_is_rejected(
    core: Core, fake: FakeProvider, http: httpx.AsyncClient
) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    headers, body = fake.checkout_webhook(session, succeeded=True)
    headers["X-Fake-Signature"] = "sha256=" + "0" * 64

    response = await http.post("/webhooks/fake", content=body, headers=headers)

    assert response.status_code == 400
    assert await payments(core) == []


async def test_webhooks_for_providers_that_are_not_enabled_are_not_found(
    http: httpx.AsyncClient,
) -> None:
    assert (await http.post("/webhooks/yookassa", content=b"{}")).status_code == 404
    assert (await http.post("/webhooks/stripe", content=b"{}")).status_code == 404


async def test_oversized_webhooks_are_refused(http: httpx.AsyncClient) -> None:
    response = await http.post("/webhooks/fake", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413


async def test_a_webhook_that_keeps_losing_races_asks_for_a_retry(
    core: Core, fake: FakeProvider, http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    headers, body = fake.checkout_webhook(session, succeeded=True)

    async def busy(*args: object) -> None:
        raise ConcurrentUpdateError("still losing")

    monkeypatch.setattr(payments_route, "apply_event", busy)
    response = await http.post("/webhooks/fake", content=body, headers=headers)

    assert response.status_code == 503
