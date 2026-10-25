"""Join requests to club chats: approve members with access, decline everyone else.

The decision itself is :func:`paidclub.access.decide_join_request` (it is also logged there).
On a decline the bot first messages the person the plans: the Bot API allows writing to
``user_chat_id`` only for five minutes and only until the request is processed, so the order
matters. A failed message never blocks the decline.

Telegram delivers a join request once, and the Bot API has no way to list pending ones, so
every call is retried through flood control (429) and network or server errors
(:func:`with_retries`). If Telegram still does not take the answer, a ``join_failed`` access
event records it, and for a member with access the owner is told to approve by hand.

Since Bot API 10.1 a bot can be assigned as the chat's join-request processor; such requests
carry a ``query_id`` and must be answered with ``answerChatJoinRequestQuery`` within ten
seconds. Both kinds are handled: requests to chats that are not club chats are ignored, or
handed back to the admins ("queue") when they come as a query.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import ChatJoinRequest

from paidclub import texts
from paidclub.access import JoinDecision, decide_join_request, record_access_event
from paidclub.bot import screens
from paidclub.bot.catalog import catalog
from paidclub.bot.ui import show
from paidclub.container import Core
from paidclub.models import AccessEventKind
from paidclub.notifications import enqueue

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

#: The request was already handled (by an admin, or by us on a redelivered update).
_ALREADY_PROCESSED = ("hide_requester_missing", "user_already_participant")
#: Each Bot API call of a join request is tried this many times.
ATTEMPTS = 4
#: Flood waits up to this long are waited out; a longer one fails the call.
MAX_FLOOD_WAIT = 30.0


async def with_retries(call: Callable[[], Awaitable[Any]], *, sleep: Sleep) -> None:
    """Make a Bot API call, riding out flood control (429) and network or server errors.

    Telegram does not deliver the join request again, and the Bot API cannot list pending
    requests, so a call lost here is lost for good.
    """
    for attempt in range(1, ATTEMPTS + 1):
        try:
            await call()
            return
        except TelegramRetryAfter as exc:
            if attempt == ATTEMPTS or exc.retry_after > MAX_FLOOD_WAIT:
                raise
            await sleep(exc.retry_after)
        except (TelegramNetworkError, TelegramServerError):
            if attempt == ATTEMPTS:
                raise
            await sleep(float(2 ** (attempt - 1)))


async def on_join_request(
    request: ChatJoinRequest, bot: Bot, core: Core, *, sleep: Sleep = asyncio.sleep
) -> None:
    user = request.from_user
    decision = await decide_join_request(
        core,
        chat_id=request.chat.id,
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )
    log.info(
        "join request from %s to %s: %s (%s)",
        user.id,
        request.chat.id,
        decision.action,
        decision.reason,
    )
    if decision.action == "decline":
        draft = screens.join_declined(catalog(core), chat_title=request.chat.title)
        try:
            await with_retries(lambda: show(bot, request.user_chat_id, draft), sleep=sleep)
        except TelegramAPIError as exc:
            log.info("could not message %s before declining: %s", user.id, exc.message)
    try:
        if request.query_id is not None:
            result = "queue" if decision.action == "ignore" else decision.action
            await with_retries(lambda: request.answer_query(result=result), sleep=sleep)
        elif decision.action == "approve":
            await with_retries(request.approve, sleep=sleep)
        elif decision.action == "decline":
            await with_retries(request.decline, sleep=sleep)
    except TelegramBadRequest as exc:
        if any(marker in exc.message.lower() for marker in _ALREADY_PROCESSED):
            log.info("join request from %s to %s was already processed", user.id, request.chat.id)
            return
        await _record_failure(core, request, decision, exc)
        raise
    except TelegramAPIError as exc:
        await _record_failure(core, request, decision, exc)
        raise


async def _record_failure(
    core: Core, request: ChatJoinRequest, decision: JoinDecision, exc: TelegramAPIError
) -> None:
    """The decision is in the access log already; say that Telegram never took it."""
    detail = f"{decision.action}: {type(exc).__name__}: {exc.message}"[:500]
    log.error("join request from %s to %s: %s", request.from_user.id, request.chat.id, detail)
    now = core.clock.now()
    async with core.sessions() as session, session.begin():
        await record_access_event(
            session,
            core,
            kind=AccessEventKind.JOIN_FAILED,
            chat_id=request.chat.id,
            telegram_id=request.from_user.id,
            subscription_id=decision.subscription_id,
            detail=detail,
        )
        if decision.action == "approve" and core.settings.owner_id is not None:
            # A member with access is stuck in the chat's request list.
            await enqueue(
                session,
                key=f"owner:join_failed:{request.chat.id}:{request.from_user.id}:{now:%Y%m%d%H}",
                kind="owner_alert",
                chat_id=core.settings.owner_id,
                message=texts.owner_alert(
                    f"Не получилось одобрить заявку пользователя {request.from_user.id} в чат "
                    f"{request.chat.title or request.chat.id} ({exc.message}). У него есть "
                    "доступ: одобрите заявку вручную в списке заявок чата."
                ),
                now=now,
            )


def create_join_router(*, sleep: Sleep = asyncio.sleep) -> Router:
    router = Router(name="paidclub.join")

    async def handler(request: ChatJoinRequest, bot: Bot, core: Core) -> None:
        await on_join_request(request, bot, core, sleep=sleep)

    router.chat_join_request.register(handler)
    return router
