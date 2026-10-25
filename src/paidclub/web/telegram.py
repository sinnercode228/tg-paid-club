"""Telegram updates in webhook mode: ``POST /telegram/webhook``.

Telegram sends every update with the secret given to ``setWebhook`` in the
``X-Telegram-Bot-Api-Secret-Token`` header; a request without it is answered 401 and never
reaches a handler. A genuine update is answered 200 at once and handled in the background,
the way aiogram's own webhook handler does by default and the way polling handles updates,
so a slow handler never makes Telegram wait and send the update again.

Updates are acknowledged before they are handled. A handler that fails is logged, and the
update is not redelivered; the payment path does not depend on redelivery, because a Stars
payment that fails to apply is logged with everything needed to apply it by hand. On shutdown
:meth:`TelegramWebhook.drain` waits for the updates still being handled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from aiogram import Bot, Dispatcher
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from paidclub.config import TELEGRAM_WEBHOOK_PATH
from paidclub.web.payments import BodyTooLarge, read_body

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - a header name
#: Updates are a few kilobytes; a long message with entities stays far below this.
MAX_UPDATE_BYTES = 1024 * 1024


class TelegramWebhook:
    def __init__(
        self, dispatcher: Dispatcher, bot: Bot, secret: str, *, path: str = TELEGRAM_WEBHOOK_PATH
    ) -> None:
        if not secret:
            raise ValueError("the Telegram webhook needs a secret token")
        self.dispatcher = dispatcher
        self.bot = bot
        self.path = path
        self._secret = secret
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def router(self) -> APIRouter:
        router = APIRouter(include_in_schema=False)

        @router.post(self.path)
        async def telegram_update(request: Request) -> Response:
            given = request.headers.get(SECRET_HEADER, "")
            if not secrets.compare_digest(given.encode(), self._secret.encode()):
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
            try:
                body = await read_body(request, limit=MAX_UPDATE_BYTES)
            except BodyTooLarge:
                return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
            try:
                update = json.loads(body)
            except ValueError:
                return JSONResponse({"ok": False, "error": "not JSON"}, status_code=400)
            if not isinstance(update, dict):
                return JSONResponse({"ok": False, "error": "not an update"}, status_code=400)
            self._start(update)
            return JSONResponse({})

        return router

    def _start(self, update: dict[str, Any]) -> None:
        task = asyncio.create_task(self._handle(update))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, update: dict[str, Any]) -> None:
        try:
            await self.dispatcher.feed_raw_update(self.bot, update)
        except Exception:
            # aiogram has logged handler errors already; this covers updates it cannot parse.
            log.exception("telegram update %s was not handled", update.get("update_id"))

    async def drain(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        """Wait up to ``timeout`` seconds for the updates being handled, then cancel the rest."""
        if not self._tasks:
            return
        pending = set(self._tasks)
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            task.cancel()
        if still_running:
            log.warning(
                "cancelled %s telegram updates still running at shutdown", len(still_running)
            )
            await asyncio.gather(*still_running, return_exceptions=True)
