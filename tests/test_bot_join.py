from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerChatJoinRequestQuery,
    ApproveChatJoinRequest,
    DeclineChatJoinRequest,
    SendMessage,
)

from paidclub.billing.fake import FakeProvider
from paidclub.bot.app import create_dispatcher
from paidclub.container import Core
from paidclub.models import AccessEventKind
from tests.botapi import TEST_TOKEN, MockedSession, buttons_of, feed, join_request_update
from tests.helpers import CHAT_ID, OWNER_ID, access_events, subscribe

MEMBER = 701
OTHER_CHAT = -1009999999999


@pytest.fixture
def session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def bot(session: MockedSession) -> Bot:
    return Bot(TEST_TOKEN, session=session)


@pytest.fixture
def dp(core: Core) -> Dispatcher:
    return create_dispatcher(core)


async def test_a_member_with_access_is_approved_without_a_message(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    await subscribe(core, fake, MEMBER)
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))
    assert session.api_methods() == ["approveChatJoinRequest"]
    [approve] = session.calls(ApproveChatJoinRequest)
    assert (approve.chat_id, approve.user_id) == (CHAT_ID, MEMBER)
    assert (await access_events(core))[-1].kind is AccessEventKind.JOIN_APPROVED


async def test_a_stranger_gets_the_plans_first_and_is_then_declined(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER, username="guest", title="Кухня"))
    # The private chat is only open until the request is processed, so the order matters.
    assert session.api_methods() == ["sendMessage", "declineChatJoinRequest"]
    [message] = session.calls(SendMessage)
    assert message.chat_id == MEMBER  # the request's user_chat_id
    assert message.text.startswith("Чтобы вступить в «Кухня», нужна активная подписка.")
    assert buttons_of(message)[0].text == "1 месяц — 990 ₽"
    [event] = await access_events(core)
    assert (event.kind, event.telegram_id) == (AccessEventKind.JOIN_DECLINED, MEMBER)


async def test_a_message_that_cannot_be_delivered_does_not_stop_the_decline(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    session.add_error(SendMessage, 403, "Forbidden: bot can't initiate conversation with a user")
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))
    assert len(session.calls(DeclineChatJoinRequest)) == 1


async def test_requests_to_chats_that_are_not_club_chats_are_left_alone(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    await feed(dp, bot, join_request_update(OTHER_CHAT, MEMBER))
    assert session.requests == []
    assert await access_events(core) == []


async def test_the_owner_is_approved_without_a_subscription(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    await feed(dp, bot, join_request_update(CHAT_ID, OWNER_ID))
    assert session.api_methods() == ["approveChatJoinRequest"]


async def test_join_request_queries_are_answered_with_the_same_decision(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    await subscribe(core, fake, MEMBER)
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER, query_id="q1"))
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER + 1, query_id="q2"))
    await feed(dp, bot, join_request_update(OTHER_CHAT, MEMBER, query_id="q3"))

    answers = [
        (a.chat_join_request_query_id, a.result) for a in session.calls(AnswerChatJoinRequestQuery)
    ]
    assert answers == [("q1", "approve"), ("q2", "decline"), ("q3", "queue")]
    assert session.calls(ApproveChatJoinRequest) == []
    assert session.calls(DeclineChatJoinRequest) == []
    [message] = session.calls(SendMessage)
    assert message.chat_id == MEMBER + 1


async def test_a_request_that_was_already_processed_is_not_an_error(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    await subscribe(core, fake, MEMBER)
    session.add_error(ApproveChatJoinRequest, 400, "Bad Request: HIDE_REQUESTER_MISSING")
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))
    assert len(session.calls(ApproveChatJoinRequest)) == 1


async def test_other_errors_while_approving_propagate(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    await subscribe(core, fake, MEMBER)
    session.add_error(ApproveChatJoinRequest, 400, "Bad Request: CHAT_ADMIN_REQUIRED")
    with pytest.raises(TelegramBadRequest):
        await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))
