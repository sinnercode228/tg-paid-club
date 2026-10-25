"""Club chats: checking the bot's rights and keeping the one join-request invite link.

Every club chat gets a single invite link created with ``creates_join_request=True``. It is
created once and stored in ``club_chats``; later starts reuse it, so a link posted somewhere
keeps working and links do not pile up in the chat settings. Each check confirms with
Telegram that the stored link still works (``editChatInviteLink`` with its own settings): one
an admin revoked is replaced by a new one. Whoever follows it sends a join request, which
:mod:`paidclub.bot.join` approves or declines by subscription.

The check runs at startup and again whenever the bot's own status in a club chat changes
(``my_chat_member``), so adding the bot as an admin after it started is enough. Problems go
to the owner through the outbox, once a day per chat. Besides the bot's rights, the check
reports settings that let people in without a join request: a public username, and in
groups the default permission of members to add people.

Those are not the only ways around the link, so the bot also watches joins
(``chat_member``): whoever joins without access, through another link or added by someone,
is removed again. Members who were in the chat before the bot was set up are not touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberOwner,
    ChatMemberRestricted,
    ChatMemberUpdated,
)
from aiogram.types.chat_member_union import ChatMemberUnion
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import texts
from paidclub.access import RevokeOutcome, may_stay, record_access_event, requeue_removals
from paidclub.bot.revoker import TelegramAccessRevoker
from paidclub.container import Core
from paidclub.db import in_transaction
from paidclub.models import AccessEventKind, ClubChat
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
            problems.extend(_ways_around_the_join_request(chat))
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


def _ways_around_the_join_request(chat: ChatFullInfo) -> list[str]:
    """Settings that let people into the chat without a join request."""
    problems = []
    if chat.username:
        problems.append(
            f"у чата есть публичное имя @{chat.username}: по нему вступают без заявки и "
            "без подписки. Сделайте чат частным"
        )
    permissions = chat.permissions
    if (
        chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        and permissions is not None
        and permissions.can_invite_users
    ):
        problems.append(
            "участники могут сами добавлять людей и делиться основной ссылкой чата, минуя "
            "заявки: в разрешениях участников запретите добавлять участников"
        )
    return problems


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


def _is_in(member: ChatMemberUnion) -> bool:
    if isinstance(member, ChatMemberRestricted):
        return member.is_member
    return not isinstance(member, ChatMemberLeft | ChatMemberBanned)


async def on_member_changed(update: ChatMemberUpdated, bot: Bot, core: Core) -> None:
    """Someone joined a club chat. Members approved by the bot have access; anyone who got in
    another way (an old invite link, added by a member or an admin) without access is
    removed, so the join request stays the only way in."""
    joined = _is_in(update.new_chat_member) and not _is_in(update.old_chat_member)
    user = update.new_chat_member.user
    if not joined or user.is_bot or update.chat.id not in core.settings.club_chat_ids:
        return
    if isinstance(update.new_chat_member, ChatMemberOwner | ChatMemberAdministrator):
        return
    decision = await may_stay(
        core,
        chat_id=update.chat.id,
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )
    if decision.action != "decline":
        return
    how = "по заявке" if update.via_join_request else "без заявки"
    if update.invite_link is not None:
        how += f", ссылка {update.invite_link.name or update.invite_link.invite_link}"
    outcome = await TelegramAccessRevoker(bot).revoke(update.chat.id, user.id)
    log.warning(
        "%s joined club chat %s without access (%s): %s", user.id, update.chat.id, how, outcome
    )
    now = core.clock.now()
    async with core.sessions() as session, session.begin():
        await record_access_event(
            session,
            core,
            kind=(
                AccessEventKind.REMOVED_ON_JOIN
                if outcome is RevokeOutcome.REMOVED
                else AccessEventKind.NO_RIGHTS
                if outcome is RevokeOutcome.NO_RIGHTS
                else AccessEventKind.NOT_PARTICIPANT
            ),
            chat_id=update.chat.id,
            telegram_id=user.id,
            detail=f"joined without access ({how})",
        )
        if core.settings.owner_id is not None:
            removed = "бот его удалил" if outcome is RevokeOutcome.REMOVED else "удалить не вышло"
            await enqueue(
                session,
                key=f"owner:joined_without_access:{update.chat.id}:{now:%Y-%m-%d}",
                kind="owner_alert",
                chat_id=core.settings.owner_id,
                message=texts.owner_alert(
                    f"В чат {update.chat.title or update.chat.id} вступил пользователь "
                    f"{user.id} без подписки ({how}), {removed}. Если в чате есть другие "
                    "ссылки-приглашения, отзовите их; участникам запретите добавлять людей."
                ),
                now=now,
            )


def create_chats_router() -> Router:
    router = Router(name="paidclub.chats")
    router.my_chat_member.register(on_bot_membership_changed)
    # Registering it makes "chat_member" part of allowed_updates (polling and webhook).
    router.chat_member.register(on_member_changed)
    return router
