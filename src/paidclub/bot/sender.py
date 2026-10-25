"""The outbox's ``send`` callable: delivers one stored message through the Bot API.

:class:`~paidclub.notifications.OutboxSender` decides when to send and retry; this adapter only
translates Telegram's answers into the outbox's vocabulary:

* 429 (flood control) -> :class:`~paidclub.notifications.RetryAfter`, the whole outbox pauses;
* 403 (bot blocked, user deactivated) and "chat not found" ->
  :class:`~paidclub.notifications.Undeliverable`, the row is closed and the member is marked
  as having blocked the bot;
* anything else propagates and is retried with backoff.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from paidclub.bot.ui import NO_PREVIEW, keyboard
from paidclub.notifications import OutgoingMessage, RetryAfter, Undeliverable

#: Bad Request answers that mean the recipient is gone for good, not that the message is wrong.
_GONE = ("chat not found", "user is deactivated", "peer_id_invalid")


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(self, message: OutgoingMessage) -> None:
        try:
            await self._bot.send_message(
                chat_id=message.chat_id,
                text=message.text,
                parse_mode=None,
                reply_markup=keyboard(message.buttons),
                link_preview_options=NO_PREVIEW,
            )
        except TelegramRetryAfter as exc:
            raise RetryAfter(exc.retry_after) from exc
        except TelegramForbiddenError as exc:
            raise Undeliverable(exc.message) from exc
        except TelegramBadRequest as exc:
            if any(marker in exc.message.lower() for marker in _GONE):
                raise Undeliverable(exc.message) from exc
            raise
