from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import (
    CreateChatInviteLink,
    GetChat,
    GetChatMember,
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from paidclub import club
from paidclub.access import RevokeOutcome
from paidclub.billing.fake import FakeProvider
from paidclub.bot.app import create_dispatcher, on_startup
from paidclub.bot.chats import INVITE_LINK_NAME, prepare_club_chats
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.models import ClubChat
from paidclub.repo import invite_links
from paidclub.scheduler import Scheduler
from tests.botapi import (
    BOT_ID,
    TEST_TOKEN,
    MockedSession,
    chat_admin,
    feed,
    my_chat_member_update,
)
from tests.helpers import CHAT_ID, OWNER_ID, FakeRevoker, load_sub, notifications, subscribe

BOT_USER = {"id": BOT_ID, "is_bot": True, "first_name": "Club"}


@pytest.fixture
def session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def bot(session: MockedSession) -> Bot:
    return Bot(TEST_TOKEN, session=session)


def chat(chat_type: str = "channel", title: str = "Клуб") -> dict[str, Any]:
    return {
        "id": CHAT_ID,
        "type": chat_type,
        "title": title,
        "accent_color_id": 0,
        "max_reaction_count": 11,
        "accepted_gift_types": dict.fromkeys(
            (
                "unlimited_gifts",
                "limited_gifts",
                "unique_gifts",
                "premium_subscription",
                "gifts_from_channels",
            ),
            False,
        ),
    }


async def stored_chat(core: Core) -> ClubChat | None:
    async with core.sessions() as db:
        return await db.get(ClubChat, CHAT_ID)


async def owner_alerts(core: Core) -> list[str]:
    return [n.text for n in await notifications(core, "owner_alert")]


async def test_startup_creates_one_join_request_link_and_reuses_it(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    await on_startup(bot, core)
    [create] = session.calls(CreateChatInviteLink)
    assert (create.chat_id, create.creates_join_request) == (CHAT_ID, True)
    assert create.member_limit is None  # cannot be combined with join requests
    assert create.name == INVITE_LINK_NAME and len(INVITE_LINK_NAME) <= 32
    async with core.sessions() as db:
        assert await invite_links(db) == [("Клуб", "https://t.me/+join1001234567890")]
    [commands] = session.calls(SetMyCommands)
    assert [c.command for c in commands.commands] == ["start", "status"]
    assert isinstance(commands.scope, BotCommandScopeAllPrivateChats)

    await on_startup(bot, core)  # a restart
    assert len(session.calls(CreateChatInviteLink)) == 1
    assert await owner_alerts(core) == []


async def test_the_owner_menu_gets_the_member_and_owner_commands(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    stats = BotCommand(command="stats", description="Статистика")
    dp = create_dispatcher(core, owner_commands=[stats])
    await dp.emit_startup(bot=bot, **dp.workflow_data)
    general, owner = session.calls(SetMyCommands)
    assert isinstance(general.scope, BotCommandScopeAllPrivateChats)
    assert isinstance(owner.scope, BotCommandScopeChat) and owner.scope.chat_id == OWNER_ID
    assert [c.command for c in owner.commands] == ["start", "status", "stats"]


async def test_a_bot_that_is_not_an_admin_creates_no_link_and_tells_the_owner(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    session.add_result(GetChatMember, {"status": "member", "user": BOT_USER})
    [check] = await prepare_club_chats(bot, core)
    assert check.invite_link is None
    assert session.calls(CreateChatInviteLink) == []
    stored = await stored_chat(core)
    assert stored is not None and (stored.title, stored.invite_link) == ("Клуб", None)
    [alert] = await owner_alerts(core)
    assert alert.startswith("Требует внимания. Чат Клуб: бот не администратор чата")
    [notification] = await notifications(core, "owner_alert")
    assert notification.chat_id == OWNER_ID


async def test_missing_admin_rights_are_spelled_out(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    session.add_result(GetChat, chat("supergroup", "Чат клуба"))
    session.add_result(
        GetChatMember, chat_admin(BOT_USER, can_invite_users=False, can_restrict_members=False)
    )
    [check] = await prepare_club_chats(bot, core)
    assert len(check.problems) == 2
    assert not check.can_remove
    [alert] = await owner_alerts(core)
    assert "нет права приглашать пользователей" in alert
    assert "нет права блокировать участников" in alert
    assert session.calls(CreateChatInviteLink) == []


async def test_channels_have_no_separate_ban_right_to_check(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    session.add_result(GetChatMember, chat_admin(BOT_USER, can_restrict_members=False))
    [check] = await prepare_club_chats(bot, core)
    assert check.problems == ()
    assert check.can_remove


async def test_a_chat_the_bot_cannot_see_is_reported(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    session.add_error(GetChat, 400, "Bad Request: chat not found")
    [check] = await prepare_club_chats(bot, core)
    assert check.problems == (
        "бот не видит чат (Bad Request: chat not found); добавьте его в чат администратором",
    )
    assert len(await owner_alerts(core)) == 1


async def test_one_broken_chat_does_not_stop_the_others(
    settings: Settings, clock: FakeClock, bot: Bot, session: MockedSession
) -> None:
    second = CHAT_ID - 1

    def unreachable(method: TelegramMethod[Any]) -> None:
        raise TelegramNetworkError(method=method, message="Request timeout error")

    session.add_result(GetChat, unreachable)
    multi = await build_core(
        settings.model_copy(update={"club_chat_ids": [CHAT_ID, second]}), clock=clock
    )
    try:
        [check] = await prepare_club_chats(bot, multi)
    finally:
        await multi.aclose()
    assert check.chat_id == second


async def test_promoting_the_bot_creates_the_link_and_retries_blocked_removals(
    core: Core, bot: Bot, session: MockedSession, fake: FakeProvider, clock: FakeClock
) -> None:
    # A member expired while the bot could not ban anyone in the chat.
    member = 451
    sub_id = await subscribe(core, fake, member)
    await club.cancel(core, telegram_id=member, subscription_id=sub_id)
    clock.advance(timedelta(days=28))
    no_rights = FakeRevoker()
    no_rights.outcomes = {member: RevokeOutcome.NO_RIGHTS}
    await Scheduler(core, no_rights).tick()
    assert (await load_sub(core, sub_id)).access_revoked_at is not None

    dp = create_dispatcher(core)
    await feed(dp, bot, my_chat_member_update(CHAT_ID, chat_admin({}), by_user_id=OWNER_ID))
    assert len(session.calls(CreateChatInviteLink)) == 1
    assert (await load_sub(core, sub_id)).access_revoked_at is None

    revoker = FakeRevoker()
    assert (await Scheduler(core, revoker).tick()).removed == 1
    assert revoker.calls == [(CHAT_ID, member)]


async def test_membership_changes_in_other_chats_are_ignored(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    dp = create_dispatcher(core)
    await feed(dp, bot, my_chat_member_update(-100777, chat_admin({})))
    assert session.requests == []
