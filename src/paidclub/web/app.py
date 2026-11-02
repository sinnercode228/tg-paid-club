"""The FastAPI application: every route the club serves, on one app.

* ``POST /webhooks/{provider}``: payment notifications (:mod:`paidclub.web.payments`);
* ``GET``/``POST /fake-checkout/{token}``: the demo payment page, 404 unless the fake provider
  is enabled (:mod:`paidclub.web.fake_checkout`);
* ``GET /ton/pay/{comment}``: the USDT-on-TON payment page, 404 unless the TON provider is
  enabled (:mod:`paidclub.web.ton_checkout`);
* ``/admin``: the owner's dashboard and CSV export (:mod:`paidclub.web.admin`);
* ``GET /paid``: where a member lands after a provider's payment page; it sends them back to
  the bot (:mod:`paidclub.web.pages`);
* ``POST /telegram/webhook``: Telegram updates, only in webhook mode
  (:mod:`paidclub.web.telegram`);
* ``GET /healthz``: 200 while the database answers, 503 otherwise (Docker's health check).

The app holds no resources of its own: the runner owns the engine, the bot and the provider
clients and closes them, so there is no lifespan handler here.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from paidclub import __version__
from paidclub.container import Core
from paidclub.web.admin import create_admin_router
from paidclub.web.fake_checkout import create_fake_checkout_router
from paidclub.web.pages import BotUsername, create_pages_router
from paidclub.web.payments import create_webhook_router
from paidclub.web.telegram import TelegramWebhook
from paidclub.web.ton_checkout import create_ton_checkout_router

log = logging.getLogger(__name__)

HEALTH_TIMEOUT_SECONDS = 3.0


def create_app(
    core: Core,
    *,
    telegram: TelegramWebhook | None = None,
    bot_username: BotUsername | None = None,
) -> FastAPI:
    """``bot_username`` gives the page after payment its link to the bot."""
    # The API schema lists the webhook routes only; it is of no use to anyone in production.
    docs = core.settings.env != "prod"
    app = FastAPI(
        title="tg-paid-club",
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.include_router(create_webhook_router(core))
    app.include_router(create_fake_checkout_router(core))
    app.include_router(create_ton_checkout_router(core))
    app.include_router(create_admin_router(core))
    app.include_router(create_pages_router(bot_username))
    if telegram is not None:
        app.include_router(telegram.router())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS), core.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # any failure to reach the database means "unhealthy"
            log.warning("health check: database unavailable: %s", type(exc).__name__)
            return JSONResponse({"ok": False, "database": "unavailable"}, status_code=503)
        return JSONResponse({"ok": True})

    return app
