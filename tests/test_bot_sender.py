from __future__ import annotations

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

from paidclub.billing.fake import FakeProvider
from paidclub.bot.sender import TelegramSender
from paidclub.container import Core
from paidclub.models import NotificationStatus
from paidclub.notifications import (
    Button,
    OutboxSender,
    OutgoingMessage,
    RetryAfter,
    Undeliverable,
)
from paidclub.repo import member_by_telegram_id
from tests.botapi import TEST_TOKEN, MockedSession, buttons_of
from tests.helpers import add_invite_link, notifications, subscribe

MEMBER = 301


@pytest.fixture
def session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def send(session: MockedSession) -> TelegramSender:
    return TelegramSender(Bot(TEST_TOKEN, session=session))


def outgoing(*buttons: Button) -> OutgoingMessage:
    return OutgoingMessage(
        notification_id=1,
        kind="reminder",
        chat_id=MEMBER,
        text="Завтра <b>продление</b>",
        buttons=buttons,
    )


async def test_messages_go_out_as_plain_text_with_one_button_per_row(
    send: TelegramSender, session: MockedSession
) -> None:
    await send(
        outgoing(
            Button("Вступить в клуб", url="https://t.me/+club"),
            Button("Отключить автопродление", callback_data="cancel:7"),
        )
    )
    [message] = session.calls(SendMessage)
    assert (message.chat_id, message.text, message.parse_mode) == (
        MEMBER,
        "Завтра <b>продление</b>",
        None,
    )
    assert isinstance(message.reply_markup, InlineKeyboardMarkup)
    rows = message.reply_markup.inline_keyboard
    assert [[button.text for button in row] for row in rows] == [
        ["Вступить в клуб"],
        ["Отключить автопродление"],
    ]
    assert [(b.url, b.callback_data) for b in buttons_of(message)] == [
        ("https://t.me/+club", None),
        (None, "cancel:7"),
    ]


async def test_a_message_without_buttons_has_no_keyboard(
    send: TelegramSender, session: MockedSession
) -> None:
    await send(outgoing())
    assert session.calls(SendMessage)[0].reply_markup is None


async def test_flood_control_becomes_retry_after(
    send: TelegramSender, session: MockedSession
) -> None:
    session.add_error(SendMessage, 429, "Too Many Requests: retry after 17", retry_after=17)
    with pytest.raises(RetryAfter) as info:
        await send(outgoing())
    assert info.value.seconds == 17


@pytest.mark.parametrize(
    ("status", "description"),
    [
        (403, "Forbidden: bot was blocked by the user"),
        (403, "Forbidden: user is deactivated"),
        (400, "Bad Request: chat not found"),
    ],
)
async def test_recipients_that_are_gone_are_undeliverable(
    send: TelegramSender, session: MockedSession, status: int, description: str
) -> None:
    session.add_error(SendMessage, status, description)
    with pytest.raises(Undeliverable, match=description):
        await send(outgoing())


async def test_other_bad_requests_are_left_to_the_retry_logic(
    send: TelegramSender, session: MockedSession
) -> None:
    session.add_error(SendMessage, 400, "Bad Request: message is too long")
    with pytest.raises(TelegramBadRequest):
        await send(outgoing())


async def test_the_outbox_delivers_the_club_link_and_records_a_blocked_bot(
    core: Core, fake: FakeProvider, send: TelegramSender, session: MockedSession
) -> None:
    await add_invite_link(core, "https://t.me/+club")
    await subscribe(core, fake, MEMBER)
    await subscribe(core, fake, MEMBER + 1)
    session.add_result(SendMessage, delivered_message)  # the first member reads the bot
    session.add_error(SendMessage, 403, "Forbidden: bot was blocked by the user")

    report = await OutboxSender(core.sessions, core.clock, send).run_once()
    assert (report.sent, report.undeliverable) == (1, 1)
    [delivered] = [m for m in session.calls(SendMessage) if m.chat_id == MEMBER]
    assert [b.url for b in buttons_of(delivered)] == ["https://t.me/+club"]
    statuses = [n.status for n in await notifications(core, "activated")]
    assert statuses == [NotificationStatus.SENT, NotificationStatus.UNDELIVERABLE]
    async with core.sessions() as db:
        blocked = await member_by_telegram_id(db, MEMBER + 1)
        assert blocked is not None and blocked.bot_blocked_at is not None


def delivered_message(method: SendMessage) -> dict[str, object]:
    return {
        "message_id": 1,
        "date": 1767225600,
        "chat": {"id": method.chat_id, "type": "private"},
        "text": method.text,
    }
