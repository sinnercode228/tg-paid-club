from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.methods import SendMessage
from aiogram.types import Message
from fastapi import FastAPI
from pydantic import SecretStr

from paidclub.bot.app import create_dispatcher
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.db import create_engine
from paidclub.web.app import create_app
from paidclub.web.telegram import MAX_UPDATE_BYTES, SECRET_HEADER, TelegramWebhook
from tests.botapi import TEST_TOKEN, MockedSession, message_update

SECRET = "s3cret-token"
MEMBER = 801


def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def bot(session: MockedSession) -> Bot:
    return Bot(TEST_TOKEN, session=session)


@pytest.fixture
def telegram(core: Core, bot: Bot) -> TelegramWebhook:
    return TelegramWebhook(create_dispatcher(core), bot, SECRET)


@pytest.fixture
async def http(core: Core, telegram: TelegramWebhook) -> AsyncIterator[httpx.AsyncClient]:
    async with client(create_app(core, telegram=telegram)) as http:
        yield http


async def post_update(
    http: httpx.AsyncClient, update: object, secret: str | None = SECRET
) -> httpx.Response:
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    return await http.post("/telegram/webhook", content=json.dumps(update), headers=headers)


# --- composition -------------------------------------------------------------------------------


async def test_health_check_answers_while_the_database_does(core: Core) -> None:
    async with client(create_app(core)) as http:
        response = await http.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


async def test_health_check_fails_when_the_database_is_unreachable(
    settings: Settings, clock: FakeClock, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'missing' / 'club.db'}")
    broken = await build_core(settings, clock=clock, engine=engine, create_schema=False)
    try:
        async with client(create_app(broken)) as http:
            response = await http.get("/healthz")
    finally:
        await broken.aclose()
    assert response.status_code == 503
    assert response.json() == {"ok": False, "database": "unavailable"}


async def test_the_app_serves_payments_checkout_and_the_dashboard(
    settings: Settings, clock: FakeClock
) -> None:
    with_admin = settings.model_copy(update={"admin_token": SecretStr("admin-token")})
    core = await build_core(with_admin, clock=clock)
    try:
        async with client(create_app(core)) as http:
            webhook = await http.post("/webhooks/fake", content=b"{}")
            checkout = await http.get("/fake-checkout/not-a-token")
            admin = await http.get("/admin")
            telegram = await http.post("/telegram/webhook", content=b"{}")
    finally:
        await core.aclose()
    assert (webhook.status_code, webhook.json()) == (400, {"ok": False, "error": "rejected"})
    assert checkout.status_code == 404
    assert "Ссылка на оплату недействительна" in checkout.text
    assert admin.status_code == 401
    assert telegram.status_code == 404  # polling mode has no Telegram webhook route


async def test_production_hides_the_api_docs(settings: Settings, clock: FakeClock) -> None:
    prod = settings.model_copy(update={"env": "prod", "providers": ["stars"]})
    core = await build_core(prod, clock=clock)
    try:
        async with client(create_app(core)) as http:
            assert (await http.get("/docs")).status_code == 404
            assert (await http.get("/openapi.json")).status_code == 404
            # The fake checkout page is not there either without the fake provider.
            assert (await http.get("/fake-checkout/anything")).status_code == 404
    finally:
        await core.aclose()


# --- telegram webhook --------------------------------------------------------------------------


async def test_an_update_with_the_secret_is_handled(
    http: httpx.AsyncClient, telegram: TelegramWebhook, session: MockedSession
) -> None:
    response = await post_update(http, message_update(MEMBER, "/start"))

    assert (response.status_code, response.json()) == (200, {})
    await telegram.drain()
    assert [m.chat_id for m in session.calls(SendMessage)] == [MEMBER]


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x"])
async def test_a_request_without_the_secret_is_refused(
    http: httpx.AsyncClient,
    telegram: TelegramWebhook,
    session: MockedSession,
    secret: str | None,
) -> None:
    response = await post_update(http, message_update(MEMBER, "/start"), secret)

    assert response.status_code == 401
    assert telegram.in_flight == 0
    assert session.requests == []


async def test_malformed_and_oversized_bodies_are_refused(
    http: httpx.AsyncClient, telegram: TelegramWebhook
) -> None:
    headers = {SECRET_HEADER: SECRET}
    not_json = await http.post("/telegram/webhook", content=b"{oops", headers=headers)
    a_list = await http.post("/telegram/webhook", content=b"[1, 2]", headers=headers)
    huge = await http.post(
        "/telegram/webhook", content=b" " * (MAX_UPDATE_BYTES + 1), headers=headers
    )
    assert (not_json.status_code, a_list.status_code, huge.status_code) == (400, 400, 413)
    assert telegram.in_flight == 0


async def test_an_update_aiogram_cannot_read_is_acknowledged_and_logged(
    http: httpx.AsyncClient, telegram: TelegramWebhook, caplog: pytest.LogCaptureFixture
) -> None:
    response = await post_update(http, {"update_id": 5, "message": {"text": "no chat"}})

    assert response.status_code == 200  # Telegram must not resend it forever
    await telegram.drain()
    assert "telegram update 5 was not handled" in caplog.text


async def test_drain_waits_for_handlers_and_cancels_the_stuck_ones(bot: Bot) -> None:
    release = asyncio.Event()
    finished: list[str] = []
    router = Router()

    @router.message(Command("quick"))
    async def quick(message: Message) -> None:
        await asyncio.sleep(0.01)
        finished.append("quick")

    @router.message(Command("stuck"))
    async def stuck(message: Message) -> None:
        await release.wait()
        finished.append("stuck")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    webhook = TelegramWebhook(dispatcher, bot, SECRET)
    async with client(_app_with(webhook)) as http:
        for text in ("/quick", "/stuck"):
            response = await post_update(http, message_update(MEMBER, text))
            assert response.status_code == 200
    assert webhook.in_flight == 2

    await webhook.drain(timeout=0.5)

    assert finished == ["quick"]
    assert webhook.in_flight == 0


def _app_with(webhook: TelegramWebhook) -> FastAPI:
    app = FastAPI()
    app.include_router(webhook.router())
    return app


def test_the_webhook_needs_a_secret(bot: Bot) -> None:
    with pytest.raises(ValueError, match="secret"):
        TelegramWebhook(Dispatcher(), bot, "")
