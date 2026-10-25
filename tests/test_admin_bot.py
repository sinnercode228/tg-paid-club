"""``/stats`` and ``/export``: owner only, private chat only, numbers from the hand-built month."""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.methods import SendDocument, SendMessage, TelegramMethod
from aiogram.types import BufferedInputFile, Chat, Message, Update, User

from paidclub.bot.admin import USAGE, create_admin_router, format_stats, parse_days
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.stats import club_report
from tests.helpers import OWNER_ID
from tests.test_stats import build_dataset

#: 23:00 on 31 March in Moscow: "/stats 31" covers exactly the hand-built March.
END_OF_MARCH = datetime(2026, 3, 31, 20, 0, tzinfo=UTC)
STRANGER = 777


class RecordingSession(BaseSession):
    """Answers every Bot API call with a sent message and keeps the requests."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram's signature
    ) -> Any:
        self.requests.append(method)
        chat_id = getattr(method, "chat_id", 0)
        return Message(
            message_id=len(self.requests),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
        )

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - aiogram's signature
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:  # pragma: no cover - never downloads
        yield b""

    async def close(self) -> None:
        pass


_update_ids = itertools.count(1)


def command(text: str, *, user_id: int = OWNER_ID, chat_type: str = "private") -> Update:
    chat_id = user_id if chat_type == "private" else -100500
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Owner"),
            text=text,
        ),
    )


@pytest.fixture
async def bot() -> AsyncGenerator[Bot, None]:
    instance = Bot("42:TEST", session=RecordingSession())
    yield instance
    await instance.session.close()


@pytest.fixture
async def march(core: Core, clock: FakeClock) -> Core:
    await build_dataset(core)
    clock.set(END_OF_MARCH)
    return core


def sent(bot: Bot) -> list[TelegramMethod[Any]]:
    session = bot.session
    assert isinstance(session, RecordingSession)
    return session.requests


async def feed(core: Core, bot: Bot, update: Update) -> Any:
    dispatcher = Dispatcher()
    dispatcher.include_router(create_admin_router(core))
    return await dispatcher.feed_update(bot, update)


async def test_stats_reports_the_month(march: Core, bot: Bot) -> None:
    await feed(march, bot, command("/stats 31"))

    [reply] = sent(bot)
    assert isinstance(reply, SendMessage)
    assert reply.chat_id == OWNER_ID
    assert reply.parse_mode is None
    # Money.human() groups thousands with a no-break space.
    lines = reply.text.replace("\xa0", " ").splitlines()
    assert lines[0] == "Клуб на 31.03.2026 23:00 (Europe/Moscow)"
    for expected in (
        "Сейчас в клубе: 7",
        "— автопродление включено: 7",
        "MRR: 5 532 ₽ и 500 ⭐",
        "За 31 день (01.03.2026 — 31.03.2026)",
        "Новые подписки: 2",
        "Вернулись после окончания доступа: 1",
        "Продления: 3",
        "Выручка: 5 460 ₽ и 500 ⭐ (5 платежей)",
        "Неудачные списания: 6",
        "Продление не прошло: 3, оплатили в льготный период: 1",
        "Отключили автопродление: 3, включили обратно: 1",
        "Доступ закончился: 3 (не оплатили: 1, после отключения: 2)",
        "Отток: 30,0 % — ушли 3 из 10, у кого был доступ за период",
        "1 месяц: в клубе 4, выручка 2 970 ₽ и 500 ⭐, MRR 2 970 ₽ и 500 ⭐",
        "3 месяца: в клубе 1, выручка 2 490 ₽, MRR 830 ₽",
        "12 месяцев: в клубе 1, выручка 0 ₽, MRR 742 ₽",
        "legacy15: в клубе 1, выручка 0 ₽, MRR 990 ₽",
    ):
        assert expected in lines
    assert any(line.startswith("Требуют внимания: 2 платежа без зачисления") for line in lines)


async def test_stats_defaults_to_thirty_days(march: Core, bot: Bot) -> None:
    await feed(march, bot, command("/stats"))

    [reply] = sent(bot)
    assert isinstance(reply, SendMessage)
    assert "За 30 дней (02.03.2026 — 31.03.2026)" in reply.text


@pytest.mark.parametrize("text", ["/stats week", "/stats 0", "/stats 400", "/stats -5"])
async def test_stats_explains_a_bad_argument(march: Core, bot: Bot, text: str) -> None:
    await feed(march, bot, command(text))

    [reply] = sent(bot)
    assert isinstance(reply, SendMessage)
    assert reply.text == USAGE


async def test_only_the_owner_gets_answers(march: Core, bot: Bot) -> None:
    for update in (
        command("/stats", user_id=STRANGER),
        command("/export", user_id=STRANGER),
        # The owner, but in a group: the numbers must not land in a club chat.
        command("/stats", chat_type="supergroup"),
    ):
        assert await feed(march, bot, update) is UNHANDLED
    assert sent(bot) == []


async def test_without_an_owner_nobody_gets_answers(march: Core, bot: Bot) -> None:
    march.settings = march.settings.model_copy(update={"owner_id": None})
    assert await feed(march, bot, command("/stats")) is UNHANDLED
    assert sent(bot) == []


async def test_export_sends_two_csv_files(march: Core, bot: Bot) -> None:
    await feed(march, bot, command("/export"))

    subscriptions, payments = sent(bot)
    for request, name, rows in (
        (subscriptions, "subscriptions-20260331-2300.csv", 11),
        (payments, "payments-20260331-2300.csv", 16),
    ):
        assert isinstance(request, SendDocument)
        assert request.chat_id == OWNER_ID
        document = request.document
        assert isinstance(document, BufferedInputFile)
        assert document.filename == name
        assert document.data.startswith(b"\xef\xbb\xbf")
        assert len(document.data.decode("utf-8-sig").strip().splitlines()) == rows + 1
        assert request.caption is not None and "Europe/Moscow" in request.caption


async def test_empty_club(core: Core) -> None:
    text = format_stats(await club_report(core), plans=core.settings.plans).replace("\xa0", " ")

    assert "Сейчас в клубе: 0" in text
    assert "MRR: 0 ₽" in text
    assert "Выручка: 0 ₽ (0 платежей)" in text
    assert "Отток" not in text
    assert "Требуют внимания" not in text


@pytest.mark.parametrize(
    ("args", "days"),
    [(None, 30), ("", 30), (" 7 ", 7), ("366", 366), ("367", None), ("1.5", None)],
)
def test_parse_days(args: str | None, days: int | None) -> None:
    assert parse_days(args) == days
