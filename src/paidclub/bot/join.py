"""Join requests to club chats: approve members with access, decline everyone else.

The decision itself is :func:`paidclub.access.decide_join_request` (it is also logged there).
On a decline the bot first messages the person the plans: the Bot API allows writing to
``user_chat_id`` only for five minutes and only until the request is processed, so the order
matters. A failed message never blocks the decline.

Since Bot API 10.1 a bot can be assigned as the chat's join-request processor; such requests
carry a ``query_id`` and must be answered with ``answerChatJoinRequestQuery`` within ten
seconds. Both kinds are handled: requests to chats that are not club chats are ignored, or
handed back to the admins ("queue") when they come as a query.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ChatJoinRequest

from paidclub.access import decide_join_request
from paidclub.bot import screens
from paidclub.bot.catalog import catalog
from paidclub.bot.ui import show
from paidclub.container import Core

log = logging.getLogger(__name__)

#: The request was already handled (by an admin, or by us on a redelivered update).
_ALREADY_PROCESSED = ("hide_requester_missing", "user_already_participant")


async def on_join_request(request: ChatJoinRequest, bot: Bot, core: Core) -> None:
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
        try:
            draft = screens.join_declined(catalog(core), chat_title=request.chat.title)
            await show(bot, request.user_chat_id, draft)
        except TelegramAPIError as exc:
            log.info("could not message %s before declining: %s", user.id, exc.message)
    try:
        if request.query_id is not None:
            result = "queue" if decision.action == "ignore" else decision.action
            await request.answer_query(result=result)
        elif decision.action == "approve":
            await request.approve()
        elif decision.action == "decline":
            await request.decline()
    except TelegramBadRequest as exc:
        if not any(marker in exc.message.lower() for marker in _ALREADY_PROCESSED):
            raise
        log.info("join request from %s to %s was already processed", user.id, request.chat.id)


def create_join_router() -> Router:
    router = Router(name="paidclub.join")
    router.chat_join_request.register(on_join_request)
    return router
