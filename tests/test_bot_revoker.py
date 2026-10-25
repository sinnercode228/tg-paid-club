from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import BanChatMember, GetChatMember, TelegramMethod, UnbanChatMember

from paidclub import club
from paidclub.access import RevokeOutcome
from paidclub.billing.fake import FakeProvider
from paidclub.bot.revoker import BAN_SAFETY_WINDOW, TelegramAccessRevoker
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.models import AccessEventKind
from paidclub.scheduler import Scheduler
from tests.botapi import TEST_TOKEN, MockedSession, chat_admin, user
from tests.helpers import CHAT_ID, OWNER_ID, access_events, load_sub, subscribe

MEMBER = 901


@pytest.fixture
def session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def revoker(session: MockedSession, clock: FakeClock, sleeps: list[float]) -> TelegramAccessRevoker:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return TelegramAccessRevoker(Bot(TEST_TOKEN, session=session), clock=clock, sleep=sleep)


def member(status: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, "user": user(MEMBER), **fields}


def restricted(*, is_member: bool) -> dict[str, Any]:
    """A muted member (``ChatMemberRestricted``), inside the chat or not."""
    flags = (
        "can_send_messages",
        "can_send_audios",
        "can_send_documents",
        "can_send_photos",
        "can_send_videos",
        "can_send_video_notes",
        "can_send_voice_notes",
        "can_send_polls",
        "can_send_other_messages",
        "can_add_web_page_previews",
        "can_react_to_messages",
        "can_edit_tag",
        "can_change_info",
        "can_invite_users",
        "can_pin_messages",
        "can_manage_topics",
    )
    return member("restricted", is_member=is_member, until_date=0, **dict.fromkeys(flags, False))


def network_error(method: TelegramMethod[Any]) -> None:
    raise TelegramNetworkError(method=method, message="Request timeout error")


async def test_a_member_is_banned_briefly_and_unbanned_at_once(
    revoker: TelegramAccessRevoker, session: MockedSession, clock: FakeClock
) -> None:
    assert await revoker.revoke(CHAT_ID, MEMBER) is RevokeOutcome.REMOVED
    assert session.api_methods() == ["getChatMember", "banChatMember", "unbanChatMember"]
    [ban] = session.calls(BanChatMember)
    assert (ban.chat_id, ban.user_id) == (CHAT_ID, MEMBER)
    # If the unban were lost, Telegram lifts this ban by itself.
    assert ban.until_date == clock.now() + BAN_SAFETY_WINDOW
    assert ban.revoke_messages is None
    [unban] = session.calls(UnbanChatMember)
    assert unban.only_if_banned is True


@pytest.mark.parametrize(
    "answer",
    [
        member("creator", is_anonymous=False),
        chat_admin(user(MEMBER), can_restrict_members=False),
    ],
    ids=["owner", "admin"],
)
async def test_the_chat_owner_and_admins_are_never_touched(
    revoker: TelegramAccessRevoker, session: MockedSession, answer: dict[str, Any]
) -> None:
    session.add_result(GetChatMember, answer)
    assert await revoker.revoke(CHAT_ID, MEMBER) is RevokeOutcome.IS_ADMIN
    assert session.api_methods() == ["getChatMember"]


@pytest.mark.parametrize(
    "answer", [member("left"), restricted(is_member=False)], ids=["left", "restricted-outside"]
)
async def test_someone_who_is_not_in_the_chat_is_not_banned(
    revoker: TelegramAccessRevoker, session: MockedSession, answer: dict[str, Any]
) -> None:
    session.add_result(GetChatMember, answer)
    assert await revoker.revoke(CHAT_ID, MEMBER) is RevokeOutcome.NOT_PARTICIPANT
    assert session.api_methods() == ["getChatMember"]


async def test_a_restricted_member_who_is_in_the_chat_is_removed(
    revoker: TelegramAccessRevoker, session: MockedSession
) -> None:
    session.add_result(GetChatMember, restricted(is_member=True))
    assert await revoker.revoke(CHAT_ID, MEMBER) is RevokeOutcome.REMOVED


@pytest.mark.parametrize("days", [0, 7], ids=["forever", "for-a-week"])
async def test_a_ban_set_by_an_admin_is_left_in_place(
    revoker: TelegramAccessRevoker, session: MockedSession, clock: FakeClock, days: int
) -> None:
    until = 0 if days == 0 else int((clock.now() + timedelta(days=days)).timestamp())
    session.add_result(GetChatMember, member("kicked", until_date=until))
    assert await revoker.revoke(CHAT_ID, MEMBER) is RevokeOutcome.NOT_PARTICIPANT
    assert session.calls(UnbanChatMember) == []


async def test_a_removal_whose_unban_was_lost_is_finished_on_retry(
    revoker: TelegramAccessRevoker, session: MockedSession, clock: FakeClock
) -> None:
    # Our own short ban from a removal five minutes ago, still in force.
    until = int((clock.now() + BAN_SAFETY_WINDOW - timedelta(minutes=5)).timestamp())
    session.add_result(GetChatMember, member("kicked", until_date=until))
    assert await revoker.revoke(CHAT_ID, MEMBER) is RevokeOutcome.REMOVED
    assert session.api_methods() == ["getChatMember", "unbanChatMember"]


@pytest.mark.parametrize(
    ("method", "status", "description", "outcome"),
    [
        (GetChatMember, 400, "Bad Request: user not found", RevokeOutcome.NOT_PARTICIPANT),
        (GetChatMember, 400, "Bad Request: PARTICIPANT_ID_INVALID", RevokeOutcome.NOT_PARTICIPANT),
        (GetChatMember, 400, "Bad Request: chat not found", RevokeOutcome.NO_RIGHTS),
        (GetChatMember, 400, "Bad Request: member list is inaccessible", RevokeOutcome.NO_RIGHTS),
        (
            GetChatMember,
            403,
            "Forbidden: bot is not a member of the channel chat",
            RevokeOutcome.NO_RIGHTS,
        ),
        (
            BanChatMember,
            400,
            "Bad Request: not enough rights to restrict/unrestrict chat member",
            RevokeOutcome.NO_RIGHTS,
        ),
        (BanChatMember, 400, "Bad Request: CHAT_ADMIN_REQUIRED", RevokeOutcome.NO_RIGHTS),
        (
            BanChatMember,
            400,
            "Bad Request: user is an administrator of the chat",
            RevokeOutcome.IS_ADMIN,
        ),
        (BanChatMember, 400, "Bad Request: can't remove chat owner", RevokeOutcome.IS_ADMIN),
        (BanChatMember, 400, "Bad Request: USER_NOT_PARTICIPANT", RevokeOutcome.NOT_PARTICIPANT),
    ],
)
async def test_final_telegram_answers_become_outcomes(
    revoker: TelegramAccessRevoker,
    session: MockedSession,
    method: type[TelegramMethod[Any]],
    status: int,
    description: str,
    outcome: RevokeOutcome,
) -> None:
    session.add_error(method, status, description)
    assert await revoker.revoke(CHAT_ID, MEMBER) is outcome
    assert session.calls(UnbanChatMember) == []


async def test_an_unknown_error_is_raised_so_the_scheduler_retries(
    revoker: TelegramAccessRevoker, session: MockedSession
) -> None:
    session.add_error(BanChatMember, 400, "Bad Request: something new")
    with pytest.raises(TelegramBadRequest):
        await revoker.revoke(CHAT_ID, MEMBER)
    session.add_result(GetChatMember, network_error)
    with pytest.raises(TelegramNetworkError):
        await revoker.revoke(CHAT_ID, MEMBER)


async def test_the_unban_rides_out_short_trouble(
    revoker: TelegramAccessRevoker, session: MockedSession, sleeps: list[float]
) -> None:
    session.add_error(UnbanChatMember, 500, "Internal Server Error")
    session.add_error(UnbanChatMember, 429, "Too Many Requests: retry after 2", retry_after=2)
    assert await revoker.revoke(CHAT_ID, MEMBER) is RevokeOutcome.REMOVED
    assert len(session.calls(UnbanChatMember)) == 3
    assert sleeps == [1.0, 2]


async def test_an_unban_that_keeps_failing_is_raised(
    revoker: TelegramAccessRevoker, session: MockedSession, sleeps: list[float]
) -> None:
    for _ in range(3):
        session.add_result(UnbanChatMember, network_error)
    with pytest.raises(TelegramNetworkError):
        await revoker.revoke(CHAT_ID, MEMBER)
    assert sleeps == [1.0, 2.0]


async def test_a_long_flood_wait_is_left_to_the_next_tick(
    revoker: TelegramAccessRevoker, session: MockedSession, sleeps: list[float]
) -> None:
    session.add_error(UnbanChatMember, 429, "Too Many Requests: retry after 60", retry_after=60)
    with pytest.raises(TelegramRetryAfter):
        await revoker.revoke(CHAT_ID, MEMBER)
    assert sleeps == []


async def test_the_scheduler_removes_expired_members_through_the_bot(
    core: Core,
    fake: FakeProvider,
    clock: FakeClock,
    revoker: TelegramAccessRevoker,
    session: MockedSession,
) -> None:
    subs = {}
    for telegram_id in (MEMBER, OWNER_ID):
        subs[telegram_id] = await subscribe(core, fake, telegram_id)
        await club.cancel(core, telegram_id=telegram_id, subscription_id=subs[telegram_id])
    clock.advance(timedelta(days=28))  # the canceled period ends

    report = await Scheduler(core, revoker).tick()
    assert (report.expired, report.removed) == (2, 1)
    assert [ban.user_id for ban in session.calls(BanChatMember)] == [MEMBER]  # never the owner
    kinds = sorted(event.kind for event in await access_events(core))
    assert kinds == [AccessEventKind.REMOVED, AccessEventKind.SKIPPED_ADMIN]
    assert (await load_sub(core, subs[MEMBER])).access_revoked_at is not None
