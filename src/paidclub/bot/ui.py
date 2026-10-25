"""Turning a :class:`~paidclub.notifications.MessageDraft` into a Telegram message.

Shared by the handlers and the outbox sender, so a button looks the same whether it comes
from the menu or from a reminder. Texts are plain (``parse_mode=None``): member names and plan
titles never need escaping.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from paidclub.bot.screens import inline_urls
from paidclub.notifications import Button, MessageDraft

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def keyboard(buttons: Sequence[Button]) -> InlineKeyboardMarkup | None:
    """One button per row; ``None`` when there are no buttons."""
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, url=b.url, callback_data=b.callback_data)]
            for b in buttons
        ]
    )


def _description(exc: TelegramBadRequest) -> str:
    return exc.message.lower()


def rejected_button_url(exc: TelegramBadRequest) -> bool:
    """Telegram refused a link button ("BUTTON_URL_INVALID", "... URL ... is invalid")."""
    text = _description(exc)
    return "url" in text and ("invalid" in text or "wrong" in text)


async def show(bot: Bot, chat_id: int, draft: MessageDraft, *, edit: Message | None = None) -> None:
    """Show ``draft`` by editing ``edit`` in place, or as a new message in ``chat_id``.

    If Telegram rejects a link button (a ``localhost`` checkout URL in a local demo), the
    links move into the text instead of failing the whole answer.
    """
    try:
        await _show(bot, chat_id, draft, edit)
    except TelegramBadRequest as exc:
        if not rejected_button_url(exc) or not any(button.url for button in draft.buttons):
            raise
        log.warning("telegram rejected a button URL (%s); sending links as text", exc.message)
        await _show(bot, chat_id, inline_urls(draft), edit)


async def _show(bot: Bot, chat_id: int, draft: MessageDraft, edit: Message | None) -> None:
    markup = keyboard(draft.buttons)
    if edit is not None:
        try:
            await bot.edit_message_text(
                text=draft.text,
                chat_id=edit.chat.id,
                message_id=edit.message_id,
                parse_mode=None,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
            return
        except TelegramBadRequest as exc:
            text = _description(exc)
            if "message is not modified" in text:
                return  # a double tap on the same button
            if "message to edit not found" not in text and "can't be edited" not in text:
                raise
            # Too old or deleted: answer with a new message instead.
    await bot.send_message(
        chat_id=chat_id,
        text=draft.text,
        parse_mode=None,
        reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )
