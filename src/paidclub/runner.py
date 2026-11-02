"""``paidclub run``: the bot, the web app, the scheduler and the outbox in one asyncio process.

A few things depend on the order of start-up and shutdown:

* In polling mode a webhook left by an earlier deployment is removed first, because Telegram
  refuses ``getUpdates`` while one is set. Pending updates are kept.
* In webhook mode Telegram is told where to send updates only once the web server listens.
* The scheduler and the outbox call the Bot API, so the bot's HTTP session is closed only
  after both have stopped; the database engine is closed last.

SIGINT and SIGTERM set one stop event: polling stops, the web server finishes its requests,
the Telegram updates still being handled get up to 10 seconds, and the scheduler and the
outbox finish their current round. A part still running after 30 seconds is cancelled; the
rows it had claimed are freed when their lease runs out. A second signal kills the process.

When a part fails, the stop event is set for the others, so they stop the same way, and the
process exits with status 1 for a supervisor (Docker's restart policy, systemd) to start it
again.

``paidclub web`` serves the web app alone (dashboard, checkout page, payment webhooks), for
looking at a simulated database without a bot. Nothing sends the outbox then.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Coroutine, Generator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from fastapi import FastAPI

from paidclub.billing.cryptopay import CryptoPayProvider
from paidclub.billing.stars import StarsProvider, create_stars_router
from paidclub.billing.ton import TonProvider
from paidclub.billing.yookassa import YooKassaProvider
from paidclub.bot.admin import ADMIN_COMMANDS, create_admin_router
from paidclub.bot.app import create_bot, create_dispatcher
from paidclub.bot.revoker import TelegramAccessRevoker
from paidclub.bot.sender import TelegramSender
from paidclub.checks import safe_database_url
from paidclub.clock import Clock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.notifications import OutboxSender
from paidclub.scheduler import Scheduler
from paidclub.web.app import create_app
from paidclub.web.telegram import TelegramWebhook

log = logging.getLogger(__name__)

#: How long shutdown waits for Telegram updates that are still being handled.
DRAIN_SECONDS = 10.0
#: How long shutdown waits for every part to finish before cancelling what is left.
SHUTDOWN_SECONDS = 30.0


class ComponentStopped(RuntimeError):
    """A part of the process ended although nobody asked it to stop."""


@dataclass
class ClubApp:
    """Everything one process runs. Build it with :func:`build_app`, run it with :func:`serve`."""

    core: Core
    web: FastAPI
    bot: Bot | None = None
    dispatcher: Dispatcher | None = None
    scheduler: Scheduler | None = None
    outbox: OutboxSender | None = None
    telegram: TelegramWebhook | None = None
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def aclose(self) -> None:
        """Close the provider clients, then the bot session, then the database engine."""
        for close in reversed(self.closers):
            try:
                await close()
            except Exception:
                log.exception("error while closing %s", close)
        if self.bot is not None:
            await self.bot.session.close()
        await self.core.aclose()


async def build_app(
    settings: Settings,
    *,
    with_bot: bool = True,
    bot_session: BaseSession | None = None,
    clock: Clock | None = None,
) -> ClubApp:
    """Connect to the database (creating missing tables) and wire every part together.

    ``bot_session`` replaces the Bot API transport (tests use an offline one).
    """
    core = await build_core(settings, clock=clock, create_schema=True)
    closers: list[Callable[[], Awaitable[None]]] = []
    bot: Bot | None = None
    try:
        if "yookassa" in settings.providers:
            yookassa = YooKassaProvider.from_settings(settings)
            closers.append(yookassa.aclose)
            core.providers.register(yookassa)
        if "cryptopay" in settings.providers:
            cryptopay = CryptoPayProvider.from_settings(settings)
            closers.append(cryptopay.aclose)
            core.providers.register(cryptopay)
        if "ton" in settings.providers:
            ton = TonProvider.from_settings(settings)
            closers.append(ton.aclose)
            core.providers.register(ton)
        if not with_bot:
            return ClubApp(core=core, web=create_app(core), closers=closers)

        bot = create_bot(settings, session=bot_session)
        # Registered even with Stars switched off in PAIDCLUB_PROVIDERS, which only decides
        # what is sold: members who subscribed earlier are still renewed by Telegram, their
        # payments must apply, and cancelling must still reach Telegram.
        core.providers.register(StarsProvider(bot), sold="stars" in settings.providers)
        dispatcher = create_dispatcher(
            core,
            extra_routers=[create_admin_router(core), create_stars_router(core)],
            owner_commands=ADMIN_COMMANDS,
        )
        telegram = None
        if settings.bot_mode == "webhook":
            assert settings.telegram_webhook_secret is not None  # checked by Settings
            telegram = TelegramWebhook(
                dispatcher, bot, settings.telegram_webhook_secret.get_secret_value()
            )
        return ClubApp(
            core=core,
            web=create_app(core, telegram=telegram, bot_username=_username_of(bot)),
            bot=bot,
            dispatcher=dispatcher,
            scheduler=Scheduler(core, TelegramAccessRevoker(bot)),
            outbox=OutboxSender(
                core.sessions,
                core.clock,
                TelegramSender(bot),
                batch_size=settings.outbox_batch_size,
                lease=settings.claim_lease,
                max_attempts=settings.outbox_max_attempts,
            ),
            telegram=telegram,
            closers=closers,
        )
    except BaseException:
        for close in reversed(closers):
            await close()
        if bot is not None:
            await bot.session.close()
        await core.aclose()
        raise


def _username_of(bot: Bot) -> Callable[[], Awaitable[str | None]]:
    async def username() -> str | None:
        return (await bot.me()).username  # cached after the first getMe

    return username


# --- web server --------------------------------------------------------------------------------


class _HealthCheckFilter(logging.Filter):
    """Docker asks ``/healthz`` every 30 seconds; those requests do not belong in the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3 and args[2] == "/healthz")


_HEALTH_CHECK_FILTER = _HealthCheckFilter()


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers: the runner owns SIGINT and SIGTERM."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class WebServer:
    def __init__(self, app: FastAPI, *, host: str, port: int) -> None:
        self.host = host
        self._server = _Server(
            uvicorn.Config(app, host=host, port=port, log_config=None, lifespan="off")
        )

    @property
    def started(self) -> bool:
        return self._server.started

    @property
    def port(self) -> int:
        """The port actually listened on (``web_port=0`` picks a free one)."""
        for server in self._server.servers:
            for sock in server.sockets:
                return int(sock.getsockname()[1])
        raise RuntimeError("the web server is not listening")

    async def wait_started(self, task: asyncio.Task[None]) -> None:
        while not self._server.started:
            if task.done():
                await task  # raises why it could not start
                raise ComponentStopped("the web server stopped before it started")
            await asyncio.sleep(0.02)

    async def serve(self, stop: asyncio.Event) -> None:
        serving = asyncio.create_task(self._serve())
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            self._server.should_exit = True
            await serving
        if not stop.is_set():
            raise ComponentStopped("the web server stopped")

    async def _serve(self) -> None:
        try:
            await self._server.serve()
        except SystemExit as exc:  # uvicorn exits the process when it cannot bind
            raise ComponentStopped(
                f"the web server could not start on {self.host}:{self._server.config.port}"
            ) from exc


# --- bot ---------------------------------------------------------------------------------------


async def _poll(bot: Bot, dispatcher: Dispatcher, stop: asyncio.Event) -> None:
    """Long polling until ``stop``; the dispatcher's startup hook runs inside it."""
    await bot.delete_webhook(drop_pending_updates=False)
    polling = asyncio.create_task(
        dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
    )
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({polling, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
        # Also when cancelled because another part failed: stopping polling the way aiogram
        # expects runs the shutdown hook and leaves no getUpdates request behind.
        while not polling.done():
            try:
                await dispatcher.stop_polling()
            except RuntimeError:  # polling has not started yet; ask again
                await asyncio.sleep(0.02)
    await polling  # raises what ended it
    if not stop.is_set():
        raise ComponentStopped("polling stopped")


async def _webhook(
    app: ClubApp, server: WebServer, web: asyncio.Task[None], stop: asyncio.Event
) -> None:
    """Run the startup hook, register the webhook once the server listens, wait for ``stop``."""
    assert app.bot is not None
    assert app.dispatcher is not None
    assert app.telegram is not None
    bot, dispatcher, settings = app.bot, app.dispatcher, app.core.settings
    assert settings.telegram_webhook_secret is not None
    workflow = {"dispatcher": dispatcher, **dispatcher.workflow_data, "bot": bot}
    await dispatcher.emit_startup(**workflow)
    try:
        await server.wait_started(web)
        if stop.is_set():
            return
        await bot.set_webhook(
            url=settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        log.info("telegram webhook set to %s", settings.telegram_webhook_url)
        await stop.wait()
    finally:
        # The webhook stays registered: a new instance may already be taking over.
        await app.telegram.drain(DRAIN_SECONDS)
        await dispatcher.emit_shutdown(**workflow)


# --- the whole process -------------------------------------------------------------------------


async def serve(app: ClubApp, stop: asyncio.Event, *, server: WebServer | None = None) -> None:
    """Run every part of ``app`` until ``stop`` is set or one of them fails; then close it."""
    settings = app.core.settings
    server = server or WebServer(app.web, host=settings.web_host, port=settings.web_port)
    logging.getLogger("uvicorn.access").addFilter(_HEALTH_CHECK_FILTER)
    log.info(
        "starting: database %s, bot %s, providers %s",
        safe_database_url(settings.database_url),
        settings.bot_mode if app.bot is not None else "off",
        ", ".join(app.core.providers.names()) or "none",
    )
    web = asyncio.create_task(server.serve(stop), name="web")
    parts: dict[str, Coroutine[Any, Any, None]] = {}
    if app.bot is not None and app.dispatcher is not None:
        if app.telegram is None:
            parts["polling"] = _poll(app.bot, app.dispatcher, stop)
        else:
            parts["webhook"] = _webhook(app, server, web, stop)
    if app.scheduler is not None:
        parts["scheduler"] = _until_stopped(app.scheduler.run(stop), stop, "scheduler")
    if app.outbox is not None:
        parts["outbox"] = _until_stopped(app.outbox.run(stop), stop, "outbox")
    tasks = [web, *(asyncio.create_task(part, name=name) for name, part in parts.items())]

    first_error: BaseException | None = None
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if first_error is None and (first_error := _error_of(task)) is not None:
                log.error("%s failed, stopping the rest: %r", task.get_name(), first_error)
        # A failure stops the other parts the way a signal does: each finishes what it is
        # doing (a renewal, a message) instead of being cancelled halfway through.
        stop.set()
        if pending:
            _, late = await asyncio.wait(pending, timeout=SHUTDOWN_SECONDS)
            for task in late:
                log.warning(
                    "%s did not stop in %ss, cancelling it", task.get_name(), SHUTDOWN_SECONDS
                )
                task.cancel()
            await asyncio.gather(*late, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await app.aclose()
    for task in tasks:
        error = _error_of(task)
        if error is not None and error is not first_error:
            log.error("%s also failed while stopping: %r", task.get_name(), error)
            first_error = first_error or error
    if first_error is not None:
        raise first_error
    log.info("stopped")


def _error_of(task: asyncio.Task[None]) -> BaseException | None:
    return None if task.cancelled() else task.exception()


async def _until_stopped(loop: Awaitable[None], stop: asyncio.Event, name: str) -> None:
    await loop
    if not stop.is_set():
        raise ComponentStopped(f"the {name} stopped")


async def main(settings: Settings, *, with_bot: bool = True) -> None:
    """Build and serve the app until SIGINT or SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled = (signal.SIGINT, signal.SIGTERM)

    def on_signal(received: signal.Signals) -> None:
        log.info("received %s, stopping (send it again to exit at once)", received.name)
        stop.set()
        for sig in handled:  # the next signal gets the default behaviour back
            loop.remove_signal_handler(sig)

    for sig in handled:
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, on_signal, sig)
    app = await build_app(settings, with_bot=with_bot)
    await serve(app, stop)
