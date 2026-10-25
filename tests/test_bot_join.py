from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramServerError
from aiogram.methods import (
    AnswerChatJoinRequestQuery,
    ApproveChatJoinRequest,
    DeclineChatJoinRequest,
    GetMe,
    SendMessage,
)

from paidclub.billing.fake import FakeProvider
from paidclub.bot.app import create_dispatcher
from paidclub.container import Core
from paidclub.models import AccessEventKind
from tests.botapi import (
    TEST_TOKEN,
    MockedSession,
    buttons_of,
    feed,
    join_request_update,
    message_update,
)
from tests.helpers import CHAT_ID, OWNER_ID, access_events, notifications, subscribe

MEMBER = 701
OTHER_CHAT = -1009999999999


@pytest.fixture
def session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def bot(session: MockedSession) -> Bot:
    return Bot(TEST_TOKEN, session=session)


class Sleeps:
    def __init__(self) -> None:
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def dp(core: Core, sleeps: Sleeps) -> Dispatcher:
    return create_dispatcher(core, sleep=sleeps)


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
    assert session.api_methods() == ["getMe", "sendMessage", "declineChatJoinRequest"]
    [message] = session.calls(SendMessage)
    assert message.chat_id == MEMBER  # the request's user_chat_id
    assert message.text.startswith("Чтобы вступить в «Кухня», нужна активная подписка.")
    assert "• 1 месяц — 990 ₽" in message.text
    # The only way on starts the bot, so the join link can reach them after paying.
    [button] = buttons_of(message)
    assert (button.text, button.url, button.callback_data) == (
        "Открыть бота и выбрать тариф",
        "https://t.me/club_bot?start=join",
        None,
    )
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


async def test_flood_control_on_approve_is_waited_out_and_retried(
    core: Core,
    dp: Dispatcher,
    bot: Bot,
    session: MockedSession,
    fake: FakeProvider,
    sleeps: Sleeps,
) -> None:
    """Telegram does not deliver a join request again; a paying member must not be left in
    the request list because of a 429 during a burst of requests."""
    await subscribe(core, fake, MEMBER)
    session.add_error(
        ApproveChatJoinRequest, 429, "Too Many Requests: retry after 3", retry_after=3
    )

    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))

    assert len(session.calls(ApproveChatJoinRequest)) == 2
    assert sleeps.waited == [3]
    assert [event.kind for event in await access_events(core)] == [AccessEventKind.JOIN_APPROVED]


async def test_flood_control_on_the_plans_message_is_waited_out_too(
    dp: Dispatcher, bot: Bot, session: MockedSession, sleeps: Sleeps
) -> None:
    session.add_error(SendMessage, 429, "Too Many Requests: retry after 2", retry_after=2)
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))
    assert session.api_methods() == [
        "getMe",
        "sendMessage",
        "sendMessage",
        "declineChatJoinRequest",
    ]


async def test_an_approve_telegram_never_takes_is_recorded_and_reaches_the_owner(
    core: Core,
    dp: Dispatcher,
    bot: Bot,
    session: MockedSession,
    fake: FakeProvider,
    sleeps: Sleeps,
) -> None:
    await subscribe(core, fake, MEMBER)
    for _ in range(4):
        session.add_error(ApproveChatJoinRequest, 502, "Bad Gateway")

    with pytest.raises(TelegramServerError):
        await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))

    assert len(session.calls(ApproveChatJoinRequest)) == 4
    assert sleeps.waited == [1, 2, 4]
    approved, failed = await access_events(core)
    assert (approved.kind, failed.kind) == (
        AccessEventKind.JOIN_APPROVED,
        AccessEventKind.JOIN_FAILED,
    )
    assert failed.detail is not None and failed.detail.startswith("approve: TelegramServerError")
    [alert] = await notifications(core, "owner_alert")
    assert str(MEMBER) in alert.text and "вручную" in alert.text


async def test_a_flood_wait_longer_than_half_a_minute_is_not_waited_out(
    core: Core,
    dp: Dispatcher,
    bot: Bot,
    session: MockedSession,
    fake: FakeProvider,
    sleeps: Sleeps,
) -> None:
    await subscribe(core, fake, MEMBER)
    session.add_error(ApproveChatJoinRequest, 429, "Too Many Requests", retry_after=120)
    with pytest.raises(TelegramRetryAfter):
        await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))
    assert sleeps.waited == []
    assert (await access_events(core))[-1].kind is AccessEventKind.JOIN_FAILED


async def test_without_the_bot_username_the_plans_come_as_buttons(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    session.add_error(GetMe, 502, "Bad Gateway")
    await feed(dp, bot, join_request_update(CHAT_ID, MEMBER))
    [message] = session.calls(SendMessage)
    assert buttons_of(message)[0].text == "1 месяц — 990 ₽"


async def test_the_start_link_from_that_message_opens_the_plans(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    await feed(dp, bot, message_update(MEMBER, "/start join"))
    [message] = session.calls(SendMessage)
    assert "Тарифы:" in message.text
