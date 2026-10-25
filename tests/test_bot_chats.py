from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import (
    CreateChatInviteLink,
    EditChatInviteLink,
    GetChat,
    GetChatMember,
    SetMyCommands,
    TelegramMethod,
    UnbanChatMember,
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
from paidclub.models import AccessEventKind, ClubChat
from paidclub.repo import invite_links
from paidclub.scheduler import Scheduler
from tests.botapi import (
    BOT_ID,
    TEST_TOKEN,
    MockedSession,
    chat_admin,
    chat_member_update,
    feed,
    my_chat_member_update,
)
from tests.helpers import (
    CHAT_ID,
    OWNER_ID,
    FakeRevoker,
    access_events,
    load_sub,
    notifications,
    subscribe,
)

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
    [check] = session.calls(EditChatInviteLink)  # still there, still by join request
    assert (check.invite_link, check.name, check.creates_join_request) == (
        "https://t.me/+join1001234567890",
        INVITE_LINK_NAME,
        True,
    )
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


def revoked(link: str) -> dict[str, Any]:
    return {
        "invite_link": link,
        "creator": BOT_USER,
        "creates_join_request": True,
        "is_primary": False,
        "is_revoked": True,
    }


@pytest.mark.parametrize(
    "answer",
    ["revoked", "bad request"],
)
async def test_a_link_an_admin_revoked_is_replaced(
    core: Core, bot: Bot, session: MockedSession, answer: str
) -> None:
    await prepare_club_chats(bot, core)
    old = "https://t.me/+join1001234567890"
    if answer == "revoked":
        session.add_result(EditChatInviteLink, revoked(old))
    else:
        session.add_error(EditChatInviteLink, 400, "Bad Request: INVITE_HASH_EXPIRED")
    new = {
        "invite_link": "https://t.me/+fresh",
        "creator": BOT_USER,
        "creates_join_request": True,
        "is_primary": False,
        "is_revoked": False,
    }
    session.add_result(CreateChatInviteLink, new)

    [check] = await prepare_club_chats(bot, core)

    assert check.invite_link == "https://t.me/+fresh"
    assert len(session.calls(CreateChatInviteLink)) == 2
    async with core.sessions() as db:
        assert await invite_links(db) == [("Клуб", "https://t.me/+fresh")]


async def test_a_revoked_link_that_cannot_be_replaced_is_not_offered(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    await prepare_club_chats(bot, core)
    session.add_result(EditChatInviteLink, revoked("https://t.me/+join1001234567890"))
    session.add_error(CreateChatInviteLink, 400, "Bad Request: CHAT_ADMIN_REQUIRED")

    [check] = await prepare_club_chats(bot, core)

    assert check.invite_link is None
    async with core.sessions() as db:
        assert await invite_links(db) == []
    assert any("ссылку-приглашение" in alert for alert in await owner_alerts(core))


# --- ways into the chat around the join request ------------------------------------------------


def with_settings(base: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**base, **extra}


async def test_a_supergroup_whose_members_may_add_people_is_reported(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    """Telegram's default for groups: every member may add people and share the primary link,
    and neither needs a join request."""
    session.add_result(
        GetChat,
        with_settings(
            chat("supergroup"), permissions={"can_send_messages": True, "can_invite_users": True}
        ),
    )
    session.add_result(GetChatMember, chat_admin(BOT_USER))
    [check] = await prepare_club_chats(bot, core)
    [problem] = check.problems
    assert "запретите добавлять участников" in problem


async def test_a_supergroup_where_only_admins_add_people_is_fine(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    session.add_result(
        GetChat, with_settings(chat("supergroup"), permissions={"can_invite_users": False})
    )
    session.add_result(GetChatMember, chat_admin(BOT_USER))
    [check] = await prepare_club_chats(bot, core)
    assert check.problems == ()


async def test_a_public_club_chat_is_reported(core: Core, bot: Bot, session: MockedSession) -> None:
    """A chat with a username is joined from t.me/<username> without a join request."""
    session.add_result(GetChat, with_settings(chat("channel"), username="open_club"))
    session.add_result(GetChatMember, chat_admin(BOT_USER))
    [check] = await prepare_club_chats(bot, core)
    [problem] = check.problems
    assert "@open_club" in problem
    [alert] = await owner_alerts(core)
    assert "Сделайте чат частным" in alert


async def test_the_bot_asks_telegram_for_member_joins(core: Core) -> None:
    assert "chat_member" in create_dispatcher(core).resolve_used_update_types()


async def test_someone_who_joins_without_access_is_removed_again(
    core: Core, bot: Bot, session: MockedSession
) -> None:
    dp = create_dispatcher(core)
    await feed(
        dp,
        bot,
        chat_member_update(CHAT_ID, 4321, by_user_id=777, invite_link="https://t.me/+primary"),
    )

    assert session.api_methods() == ["getChatMember", "banChatMember", "unbanChatMember"]
    [unban] = session.calls(UnbanChatMember)
    assert (unban.user_id, unban.only_if_banned) == (4321, True)
    [event] = await access_events(core)
    assert (event.kind, event.telegram_id, event.chat_id) == (
        AccessEventKind.REMOVED_ON_JOIN,
        4321,
        CHAT_ID,
    )
    assert event.detail is not None and "https://t.me/+primary" in event.detail
    [alert] = await owner_alerts(core)
    assert "4321" in alert and "отзовите" in alert


async def test_members_with_access_admins_and_bots_who_join_are_left_alone(
    core: Core, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    await subscribe(core, fake, 4322)
    dp = create_dispatcher(core)
    for update in (
        chat_member_update(CHAT_ID, 4322, via_join_request=True),  # approved by the bot
        chat_member_update(CHAT_ID, OWNER_ID),
        chat_member_update(CHAT_ID, 4323, new_member=chat_admin({})),
        chat_member_update(CHAT_ID, 4324, old="member", new="left"),  # leaving
        chat_member_update(-100777, 4325),  # not a club chat
    ):
        await feed(dp, bot, update)
    assert session.requests == []
    assert await access_events(core) == []
