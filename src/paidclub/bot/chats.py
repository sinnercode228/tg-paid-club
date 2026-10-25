"""Club chats: checking the bot's rights and keeping the one join-request invite link.

Every club chat gets a single invite link created with ``creates_join_request=True``. It is
created once and stored in ``club_chats``; later starts reuse it, so a link posted somewhere
keeps working and links do not pile up in the chat settings. Each check confirms with
Telegram that the stored link still works (``editChatInviteLink`` with its own settings): one
an admin revoked is replaced by a new one. Whoever follows it sends a join request, which
:mod:`paidclub.bot.join` approves or declines by subscription.

The check runs at startup and again whenever the bot's own status in a club chat changes
(``my_chat_member``), so adding the bot as an admin after it started is enough. Problems go
to the owner through the outbox, once a day per chat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberAdministrator, ChatMemberUpdated
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import texts
from paidclub.access import requeue_removals
from paidclub.container import Core
from paidclub.db import in_transaction
from paidclub.models import ClubChat
from paidclub.notifications import enqueue

log = logging.getLogger(__name__)

#: Shown to the owner in the chat's invite link list (0-32 characters).
INVITE_LINK_NAME = "Подписчики клуба (по заявке)"


@dataclass(frozen=True, slots=True)
class ChatCheck:
    chat_id: int
    title: str | None
    invite_link: str | None
    #: Owner-facing descriptions of what is wrong; empty when the chat is fully set up.
    problems: tuple[str, ...]
    #: The bot can remove members here, so removals blocked earlier were re-queued.
    can_remove: bool


async def prepare_club_chats(bot: Bot, core: Core) -> list[ChatCheck]:
    """Check every configured club chat; one broken chat does not stop the others."""
    checks = []
    for chat_id in core.settings.club_chat_ids:
        try:
            checks.append(await prepare_chat(bot, core, chat_id))
        except Exception:
            log.exception("could not prepare club chat %s", chat_id)
    return checks


async def prepare_chat(bot: Bot, core: Core, chat_id: int) -> ChatCheck:
    problems: list[str] = []
    title: str | None = None
    can_invite = can_remove = False
    try:
        chat = await bot.get_chat(chat_id=chat_id)
        me = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        problems.append(f"бот не видит чат ({exc.message}); добавьте его в чат администратором")
    else:
        title = chat.title
        if not isinstance(me, ChatMemberAdministrator):
            problems.append(
                "бот не администратор чата, поэтому не может одобрять заявки и удалять "
                "участников с закончившейся подпиской"
            )
        else:
            can_invite = me.can_invite_users
            if not can_invite:
                problems.append(
                    "у бота нет права приглашать пользователей: без него он не создаст ссылку "
                    "и не сможет одобрять заявки"
                )
            # Channels have no separate "ban users" right for admins; supergroups do.
            can_remove = chat.type == ChatType.CHANNEL or me.can_restrict_members
            if not can_remove:
                problems.append(
                    "у бота нет права блокировать участников: без него он не сможет удалить "
                    "тех, у кого закончилась подписка"
                )

    stored = await _stored_link(core, chat_id)
    link = stored
    if link is not None and can_invite:
        link = await _still_valid(bot, chat_id, link)
    if link is None and can_invite:
        try:
            created = await bot.create_chat_invite_link(
                chat_id=chat_id, name=INVITE_LINK_NAME, creates_join_request=True
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            problems.append(f"не получилось создать ссылку-приглашение ({exc.message})")
        else:
            link = created.invite_link
            log.info("created join-request invite link for chat %s", chat_id)

    async def save(session: AsyncSession) -> str | None:
        now = core.clock.now()
        row = await session.get(ClubChat, chat_id)
        if row is None:
            row = ClubChat(chat_id=chat_id, title=title, invite_link=link, updated_at=now)
            session.add(row)
        else:
            if title is not None:
                row.title = title
            if row.invite_link is None or link != stored:
                # A new link, or none: the stored one was revoked and could not be replaced.
                row.invite_link = link
            row.updated_at = now
        if problems and core.settings.owner_id is not None:
            await enqueue(
                session,
                key=f"owner:chat_setup:{chat_id}:{now:%Y-%m-%d}",
                kind="owner_alert",
                chat_id=core.settings.owner_id,
                message=texts.owner_alert(f"Чат {title or chat_id}: " + "; ".join(problems) + "."),
                now=now,
            )
        await session.flush()
        return row.invite_link

    link = await in_transaction(core.sessions, save)
    for problem in problems:
        log.warning("club chat %s: %s", chat_id, problem)
    if can_remove:
        await requeue_removals(core, chat_id)
    return ChatCheck(chat_id, title, link, tuple(problems), can_remove)


async def _still_valid(bot: Bot, chat_id: int, link: str) -> str | None:
    """``link`` if Telegram still has it as a working join-request link, otherwise ``None``.

    Editing the link with its own settings answers both questions: it fails or comes back
    revoked when an admin revoked it, and it switches join requests back on if someone
    turned them off.
    """
    try:
        edited = await bot.edit_chat_invite_link(
            chat_id=chat_id, invite_link=link, name=INVITE_LINK_NAME, creates_join_request=True
        )
    except TelegramBadRequest as exc:
        log.warning("invite link of chat %s is no longer usable: %s", chat_id, exc.message)
        return None
    if edited.is_revoked:
        log.warning("invite link of chat %s was revoked", chat_id)
        return None
    return edited.invite_link


async def _stored_link(core: Core, chat_id: int) -> str | None:
    async with core.sessions() as session:
        row = await session.get(ClubChat, chat_id)
        return row.invite_link if row is not None else None


async def on_bot_membership_changed(update: ChatMemberUpdated, bot: Bot, core: Core) -> None:
    """The bot was added, promoted, restricted or removed in some chat."""
    if update.chat.id not in core.settings.club_chat_ids:
        return
    log.info("bot status in club chat %s is now %s", update.chat.id, update.new_chat_member.status)
    await prepare_chat(bot, core, update.chat.id)


def create_chats_router() -> Router:
    router = Router(name="paidclub.chats")
    router.my_chat_member.register(on_bot_membership_changed)
    return router
