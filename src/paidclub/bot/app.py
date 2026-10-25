"""Building the aiogram ``Bot`` and ``Dispatcher`` for the club.

The run command wires it like this::

    core = await build_core(settings)
    bot = create_bot(settings)
    dp = create_dispatcher(
        core,
        extra_routers=[create_admin_router(core), create_stars_router(core)],
        owner_commands=ADMIN_COMMANDS,
    )
    scheduler = Scheduler(core, TelegramAccessRevoker(bot))
    outbox = OutboxSender(core.sessions, core.clock, TelegramSender(bot), ...)
    await dp.start_polling(bot)   # plus scheduler.run(stop) and outbox.run(stop)

On startup the dispatcher checks every club chat, creates (once) the join-request invite
link and sets the command menus. ``core`` reaches every handler as a keyword argument.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from paidclub.bot.chats import create_chats_router, prepare_club_chats
from paidclub.bot.join import create_join_router
from paidclub.bot.member import create_member_router
from paidclub.bot.revoker import TelegramAccessRevoker
from paidclub.bot.sender import TelegramSender
from paidclub.config import Settings
from paidclub.container import Core

__all__ = [
    "MEMBER_COMMANDS",
    "TelegramAccessRevoker",
    "TelegramSender",
    "create_bot",
    "create_dispatcher",
    "on_startup",
]

log = logging.getLogger(__name__)

MEMBER_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Тарифы и оплата"),
    BotCommand(command="status", description="Моя подписка"),
)


def create_bot(settings: Settings, *, session: BaseSession | None = None) -> Bot:
    if settings.bot_token is None:
        raise RuntimeError("PAIDCLUB_BOT_TOKEN is not set")
    return Bot(
        settings.bot_token.get_secret_value(),
        session=session,
        # Every text the bot sends is plain; nothing in it should be parsed as markup.
        default=DefaultBotProperties(parse_mode=None, link_preview_is_disabled=True),
    )


def create_dispatcher(
    core: Core,
    *,
    extra_routers: Sequence[Router] = (),
    owner_commands: Sequence[BotCommand] = (),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Dispatcher:
    """A dispatcher with the club's routers.

    ``extra_routers`` (owner commands, Telegram Stars payments) are included first, ahead of
    the member router's catch-all text handler. ``owner_commands`` are added to the owner's
    command menu on startup, next to the member commands. ``sleep`` is how handlers wait out
    Telegram's flood control (tests pass one that returns at once).
    """
    dp = Dispatcher(core=core, owner_commands=tuple(owner_commands))
    dp.include_routers(
        *extra_routers,
        create_join_router(sleep=sleep),
        create_chats_router(),
        create_member_router(),
    )
    dp.startup.register(on_startup)
    return dp


async def on_startup(bot: Bot, core: Core, owner_commands: Sequence[BotCommand] = ()) -> None:
    if not core.settings.club_chat_ids:
        log.warning("PAIDCLUB_CLUB_CHAT_IDS is empty: there is no chat to grant access to")
    for check in await prepare_club_chats(bot, core):
        if check.problems:
            log.warning("club chat %s needs attention: %s", check.chat_id, check.problems)
        else:
            log.info(
                "club chat %s (%s) is ready: %s", check.chat_id, check.title, check.invite_link
            )
    await set_commands(bot, core, owner_commands)


async def set_commands(bot: Bot, core: Core, owner_commands: Sequence[BotCommand] = ()) -> None:
    """Member commands in every private chat; the owner also gets ``owner_commands``.

    A chat-scoped list replaces the general one for that chat, so the owner's list repeats
    the member commands. A failure here only costs the menu, never the start.
    """
    try:
        await bot.set_my_commands(
            commands=list(MEMBER_COMMANDS), scope=BotCommandScopeAllPrivateChats()
        )
        owner_id = core.settings.owner_id
        if owner_commands and owner_id is not None:
            await bot.set_my_commands(
                commands=[*MEMBER_COMMANDS, *owner_commands],
                scope=BotCommandScopeChat(chat_id=owner_id),
            )
    except TelegramAPIError as exc:
        log.warning("could not set the bot command menu: %s", exc.message)
