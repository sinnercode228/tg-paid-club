"""``paidclub run`` end to end: a real uvicorn server on a free port, the real dispatcher, the
scheduler and the outbox, with the Bot API answered offline."""

from __future__ import annotations

import asyncio
import json
import socket
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import httpx
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import DeleteWebhook, GetUpdates, SendMessage, SetWebhook, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Update
from pydantic import SecretStr

from paidclub.billing.yookassa import YooKassaProvider
from paidclub.config import Settings
from paidclub.runner import ClubApp, ComponentStopped, WebServer, build_app, serve
from paidclub.web.telegram import SECRET_HEADER
from tests.botapi import TEST_TOKEN, MockedSession, message_update

MEMBER = 777
SECRET = "s3cret-token"
DEADLINE = 10.0


class PollingSession(MockedSession):
    """Answers ``getUpdates`` with the queued updates, one per call, then with nothing."""

    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.updates = deque(updates or [])

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> TelegramType:
        if not isinstance(method, GetUpdates):
            return await super().make_request(bot, method, timeout)
        self.requests.append(method)
        await asyncio.sleep(0.01)  # long polling waits; never spin the event loop
        batch = [self.updates.popleft()] if self.updates else []
        return cast(TelegramType, [Update.model_validate(u, context={"bot": bot}) for u in batch])


@pytest.fixture
def run_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "bot_token": SecretStr(TEST_TOKEN),
            "web_host": "127.0.0.1",
            "web_port": 0,
            "scheduler_interval_seconds": 0.05,
        }
    )


@pytest.fixture
def webhook_settings(run_settings: Settings) -> Settings:
    return run_settings.model_copy(
        update={
            "bot_mode": "webhook",
            "public_base_url": "https://club.example",
            "telegram_webhook_secret": SecretStr(SECRET),
        }
    )


class Running:
    """``serve`` in a background task, with the server it listens on."""

    def __init__(self, app: ClubApp) -> None:
        self.app = app
        self.stop = asyncio.Event()
        self.server = WebServer(app.web, host="127.0.0.1", port=0)
        self.task = asyncio.create_task(serve(app, self.stop, server=self.server))

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    async def started(self) -> None:
        async with asyncio.timeout(DEADLINE):
            while not self.server.started:
                if self.task.done():
                    await self.task
                await asyncio.sleep(0.01)

    async def shutdown(self) -> None:
        self.stop.set()
        async with asyncio.timeout(DEADLINE):
            await self.task


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait for state that no event announces (requests recorded by the session)."""
    async with asyncio.timeout(DEADLINE):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.fixture
async def running() -> AsyncIterator[Callable[[ClubApp], Running]]:
    started: list[Running] = []

    def start(app: ClubApp) -> Running:
        started.append(Running(app))
        return started[-1]

    yield start
    for run in started:  # a failed test must not leave a server listening
        run.stop.set()
        if not run.task.done():
            run.task.cancel()
        await asyncio.gather(run.task, return_exceptions=True)


async def test_polling_mode_runs_everything_and_stops_cleanly(
    run_settings: Settings, running: Callable[[ClubApp], Running]
) -> None:
    session = PollingSession([message_update(MEMBER, "/start")])
    app = await build_app(run_settings, bot_session=session)
    run = running(app)
    await run.started()

    await eventually(lambda: any(m.chat_id == MEMBER for m in session.calls(SendMessage)))
    async with httpx.AsyncClient(base_url=run.base_url) as http:
        health = await http.get("/healthz")
        after_payment = await http.get("/paid")
    await run.shutdown()

    assert health.status_code == 200
    assert 'href="https://t.me/club_bot"' in after_payment.text
    methods = session.api_methods()
    # A leftover webhook would make getUpdates fail with 409, so it goes first.
    assert methods[0] == "deleteWebhook"
    assert session.calls(DeleteWebhook)[0].drop_pending_updates is False
    assert methods.index("setMyCommands") < methods.index("getUpdates")
    assert "setWebhook" not in methods
    assert session.closed


async def test_webhook_mode_registers_the_webhook_once_the_server_listens(
    webhook_settings: Settings, running: Callable[[ClubApp], Running]
) -> None:
    session = MockedSession()
    app = await build_app(webhook_settings, bot_session=session)
    run = running(app)
    listening: list[bool] = []

    def set_webhook(method: TelegramMethod[Any]) -> bool:
        with socket.create_connection(("127.0.0.1", run.server.port), timeout=1):
            listening.append(True)
        return True

    session.add_result(SetWebhook, set_webhook)
    await run.started()
    await eventually(lambda: bool(session.calls(SetWebhook)))

    async with httpx.AsyncClient(base_url=run.base_url) as http:
        refused = await http.post("/telegram/webhook", content=b"{}")
        accepted = await http.post(
            "/telegram/webhook",
            content=json.dumps(message_update(MEMBER, "/start")),
            headers={SECRET_HEADER: SECRET},
        )
    await eventually(lambda: any(m.chat_id == MEMBER for m in session.calls(SendMessage)))
    await run.shutdown()

    [registered] = session.calls(SetWebhook)
    assert listening == [True]
    assert registered.url == "https://club.example/telegram/webhook"
    assert registered.secret_token == SECRET
    assert registered.drop_pending_updates is False
    assert {"message", "callback_query", "chat_join_request", "my_chat_member"} <= set(
        registered.allowed_updates or []
    )
    assert "pre_checkout_query" in (registered.allowed_updates or [])
    assert (refused.status_code, accepted.status_code) == (401, 200)
    methods = session.api_methods()
    assert "getUpdates" not in methods
    assert "deleteWebhook" not in methods  # a new instance may already be taking over
    assert "setMyCommands" in methods  # the startup hook ran
    assert session.closed


async def test_a_port_in_use_stops_the_process_and_closes_everything(
    run_settings: Settings,
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        session = PollingSession()
        app = await build_app(
            run_settings.model_copy(update={"web_port": port}), bot_session=session
        )
        with pytest.raises(ComponentStopped, match=f"could not start on 127.0.0.1:{port}"):
            async with asyncio.timeout(DEADLINE):
                await serve(app, asyncio.Event())
    assert session.closed


async def test_a_rejected_bot_token_stops_the_process(run_settings: Settings) -> None:
    session = PollingSession()
    session.add_error(DeleteWebhook, 401, "Unauthorized")
    app = await build_app(run_settings, bot_session=session)

    with pytest.raises(TelegramUnauthorizedError):
        async with asyncio.timeout(DEADLINE):
            await serve(app, asyncio.Event(), server=WebServer(app.web, host="127.0.0.1", port=0))
    assert session.closed
    assert "getUpdates" not in session.api_methods()


async def test_providers_are_registered_from_settings(run_settings: Settings) -> None:
    settings = run_settings.model_copy(
        update={
            "providers": ["fake", "yookassa", "stars"],
            "yookassa_shop_id": "123",
            "yookassa_secret_key": SecretStr("test_key"),
        }
    )
    app = await build_app(settings, bot_session=MockedSession())
    yookassa = app.core.providers.require("yookassa")
    await app.aclose()
    assert app.core.providers.names() == ["fake", "yookassa", "stars"]
    assert isinstance(yookassa, YooKassaProvider)
    assert yookassa._client.is_closed

    web_only = await build_app(settings, with_bot=False)
    try:
        # Stars needs the bot; YooKassa webhooks are still served.
        assert web_only.core.providers.names() == ["fake", "yookassa"]
        assert web_only.bot is None
        assert web_only.scheduler is None
    finally:
        await web_only.aclose()


async def test_web_only_serves_until_stopped(
    run_settings: Settings, running: Callable[[ClubApp], Running]
) -> None:
    app = await build_app(run_settings, with_bot=False)
    run = running(app)
    await run.started()
    async with httpx.AsyncClient(base_url=run.base_url) as http:
        assert (await http.get("/healthz")).status_code == 200
    await run.shutdown()
