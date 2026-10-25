from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from paidclub import callbacks, club
from paidclub.billing.fake import FakeProvider
from paidclub.bot.app import create_dispatcher
from paidclub.bot.member import GENERIC_ERROR
from paidclub.bot.screens import Go, Menu
from paidclub.bot.sender import TelegramSender
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.domain.subscriptions import SubStatus
from paidclub.notifications import OutboxSender
from paidclub.payments import apply_event
from paidclub.repo import member_by_telegram_id
from paidclub.scheduler import Scheduler
from tests.botapi import (
    TEST_TOKEN,
    MockedSession,
    buttons_of,
    callback_update,
    feed,
    message_update,
)
from tests.helpers import FakeRevoker, StubStarsProvider, add_invite_link, load_sub, subscribe

MEMBER = 501
FEB_28 = "28.02.2026"


@pytest.fixture
def session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def bot(session: MockedSession) -> Bot:
    return Bot(TEST_TOKEN, session=session)


@pytest.fixture
def dp(core: Core) -> Dispatcher:
    return create_dispatcher(core)


def last_screen(session: MockedSession) -> SendMessage | EditMessageText:
    shown = [r for r in session.requests if isinstance(r, SendMessage | EditMessageText)]
    assert shown, "the bot showed nothing"
    return shown[-1]


def labels(method: SendMessage | EditMessageText) -> list[str]:
    return [button.text for button in buttons_of(method)]


def data_of(method: SendMessage | EditMessageText, text: str) -> str:
    [button] = [b for b in buttons_of(method) if b.text == text]
    assert button.callback_data is not None
    return button.callback_data


def url_of(method: SendMessage | EditMessageText, text: str) -> str:
    [button] = [b for b in buttons_of(method) if b.text == text]
    assert button.url is not None
    return button.url


async def tap(dp: Dispatcher, bot: Bot, data: str, user_id: int = MEMBER) -> None:
    await feed(dp, bot, callback_update(user_id, data))


async def test_start_shows_a_newcomer_the_plans_and_remembers_them(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    await feed(dp, bot, message_update(MEMBER, "/start", username="anna"))

    [message] = session.calls(SendMessage)
    assert message.chat_id == MEMBER
    assert message.parse_mode is None
    assert "Тарифы:\n• 1 месяц — 990 ₽\n• 3 месяца — 2\u00a0490 ₽\n• 12 месяцев — 8\u00a0900 ₽" in (
        message.text
    )
    assert "⭐" not in message.text  # the Stars provider is not enabled
    assert labels(message) == [
        "1 месяц — 990 ₽",
        "3 месяца — 2\u00a0490 ₽",
        "12 месяцев — 8\u00a0900 ₽",
    ]
    assert data_of(message, "1 месяц — 990 ₽") == Menu(go=Go.PLAN, plan="month").pack()
    async with core.sessions() as db:
        member = await member_by_telegram_id(db, MEMBER)
        assert member is not None and member.username == "anna"


async def test_each_plan_offers_the_providers_that_have_a_price_for_it(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    core.providers.register(StubStarsProvider())
    await feed(dp, bot, message_update(MEMBER, "/start"))
    assert "• 1 месяц — 990 ₽ или 500 ⭐" in last_screen(session).text

    await tap(dp, bot, Menu(go=Go.PLAN, plan="month").pack())
    month = last_screen(session)
    assert isinstance(month, EditMessageText)
    assert month.text.startswith("Тариф «1 месяц»: 990 ₽ или 500 ⭐ за 1 месяц.")
    assert labels(month) == [
        "Тестовая оплата — 990 ₽",
        "Telegram Stars — 500 ⭐",
        "« Все тарифы",
    ]
    assert data_of(month, "Telegram Stars — 500 ⭐") == "m:buy:month:stars:0"

    await tap(dp, bot, Menu(go=Go.PLAN, plan="quarter").pack())
    quarter = last_screen(session)
    assert "продлевается каждые 3 месяца" in quarter.text
    assert labels(quarter) == ["Тестовая оплата — 2\u00a0490 ₽", "« Все тарифы"]


async def test_paying_for_a_plan_sends_the_member_the_club_link(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    await add_invite_link(core, "https://t.me/+club")
    await tap(dp, bot, Menu(go=Go.BUY, plan="month", provider="fake").pack())

    checkout = last_screen(session)
    assert isinstance(checkout, EditMessageText)  # the menu message turns into the bill
    assert checkout.text.startswith("К оплате: 990 ₽ — тариф «1 месяц».")
    assert "тестовая страница оплаты" in checkout.text
    pay_url = url_of(checkout, "Оплатить 990 ₽")
    assert pay_url.startswith("http://localhost:8000/fake-checkout/")
    [answer] = session.calls(AnswerCallbackQuery)
    assert answer.show_alert is False

    # The member pays on the fake checkout page, which sends the signed webhook.
    page = fake.read_checkout(pay_url.rsplit("/", 1)[1])
    [event] = await fake.parse_webhook(*fake.checkout_webhook(page, succeeded=True))
    await apply_event(core, event)
    report = await OutboxSender(core.sessions, core.clock, TelegramSender(bot)).run_once()
    assert report.sent == 1

    activated = last_screen(session)
    assert isinstance(activated, SendMessage)
    assert activated.chat_id == MEMBER
    assert activated.text.startswith(f"Оплата прошла, подписка «1 месяц» активна до {FEB_28}.")
    assert url_of(activated, "Вступить в клуб") == "https://t.me/+club"

    # Coming back to the bot (the provider's return URL points to it) shows the subscription.
    await feed(dp, bot, message_update(MEMBER, "/start"))
    home = last_screen(session)
    assert home.text.startswith(f"Подписка «1 месяц» активна до {FEB_28}.")
    assert f"Автопродление включено: {FEB_28} спишем 990 ₽ с сохранённой карты." in home.text
    assert labels(home) == ["Вступить в клуб", "Отключить автопродление"]


async def test_status_without_a_subscription_shows_the_plans(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    await feed(dp, bot, message_update(MEMBER, "/status"))
    message = last_screen(session)
    assert message.text.startswith("У вас пока нет подписки.")
    assert "Тарифы:" in message.text


async def test_auto_renew_can_be_turned_off_and_back_on(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await add_invite_link(core)

    await tap(dp, bot, Menu(go=Go.CANCEL, sub=sub_id).pack())
    canceled = last_screen(session)
    assert isinstance(canceled, EditMessageText)
    assert canceled.text.startswith(
        f"Автопродление отключено. Доступ к клубу по подписке «1 месяц» сохранится до {FEB_28}."
    )
    assert labels(canceled) == ["Вступить в клуб", "Включить автопродление"]
    assert fake.canceled == [sub_id]
    assert (await load_sub(core, sub_id)).status is SubStatus.CANCELED

    await tap(dp, bot, data_of(canceled, "Включить автопродление"))
    resumed = last_screen(session)
    assert resumed.text.startswith("Автопродление снова включено.\nПодписка «1 месяц» активна")
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE


async def test_buttons_in_outbox_messages_answer_with_a_new_message(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await tap(dp, bot, callbacks.cancel(sub_id))  # the button under a renewal reminder
    assert session.calls(EditMessageText) == []
    [message] = session.calls(SendMessage)
    assert message.text.startswith("Автопродление отключено.")
    assert (await load_sub(core, sub_id)).status is SubStatus.CANCELED


async def test_pay_button_after_a_failed_renewal_opens_a_checkout_for_the_next_period(
    core: Core,
    dp: Dispatcher,
    bot: Bot,
    session: MockedSession,
    fake: FakeProvider,
    clock: FakeClock,
) -> None:
    sub_id = await subscribe(core, fake, MEMBER, card="decline")
    clock.advance(timedelta(days=28))  # 28 February, the renewal date
    assert (await Scheduler(core, FakeRevoker()).tick()).charge_failed == 1

    await feed(dp, bot, message_update(MEMBER, "/status"))
    status = last_screen(session)
    assert status.text == (
        "Не получилось списать 990 ₽ за продление подписки «1 месяц».\n"
        "Доступ к клубу сохранится до 03.03.2026.\n"
        "Повторим попытку 01.03.2026."
    )
    assert labels(status) == ["Оплатить 990 ₽"]

    await tap(dp, bot, callbacks.pay(sub_id))  # the button in the "payment failed" message
    checkout = last_screen(session)
    assert isinstance(checkout, SendMessage)
    assert checkout.text.startswith("К оплате: 990 ₽ — продление подписки «1 месяц».")
    assert labels(checkout) == ["Оплатить 990 ₽", "Моя подписка"]
    page = fake.read_checkout(url_of(checkout, "Оплатить 990 ₽").rsplit("/", 1)[1])
    assert (page.subscription_id, page.for_period) == (sub_id, 2)


async def test_a_refused_action_pops_up_the_reason_next_to_the_current_status(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)

    await tap(dp, bot, callbacks.pay(sub_id))  # paying ahead is not for canceled ones
    [answer] = session.calls(AnswerCallbackQuery)
    assert answer.show_alert is True
    assert answer.text == club.ActionNotAllowed.user_message
    assert last_screen(session).text.startswith("Автопродление отключено.")

    await tap(dp, bot, Menu(go=Go.CANCEL, sub=sub_id).pack(), user_id=MEMBER + 1)
    assert session.calls(AnswerCallbackQuery)[-1].text == club.SubscriptionNotFound.user_message
    assert fake.canceled == [sub_id]  # only the member's own cancel reached the provider


async def test_buying_again_while_subscribed_shows_the_subscription(
    core: Core, dp: Dispatcher, bot: Bot, session: MockedSession, fake: FakeProvider
) -> None:
    await subscribe(core, fake, MEMBER)
    await tap(dp, bot, Menu(go=Go.BUY, plan="year", provider="fake").pack())
    screen = last_screen(session)
    assert screen.text.startswith("У вас уже есть подписка.\nПодписка «1 месяц» активна")


async def test_an_expired_member_sees_the_way_back(
    core: Core,
    dp: Dispatcher,
    bot: Bot,
    session: MockedSession,
    fake: FakeProvider,
    clock: FakeClock,
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    clock.advance(timedelta(days=28))
    await Scheduler(core, FakeRevoker()).tick()
    assert (await load_sub(core, sub_id)).status is SubStatus.EXPIRED

    await feed(dp, bot, message_update(MEMBER, "/status"))
    status = last_screen(session)
    assert status.text.startswith("Подписка «1 месяц» закончилась, доступ к клубу закрыт.")
    assert labels(status) == ["Выбрать тариф"]
    # /start goes straight to the plans for someone without a live subscription.
    await feed(dp, bot, message_update(MEMBER, "/start"))
    assert "Тарифы:" in last_screen(session).text


async def test_a_rejected_checkout_url_is_sent_as_text(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    session.add_error(
        EditMessageText,
        400,
        "Bad Request: inline keyboard button URL 'http://localhost:8000/fake-checkout/x' "
        "is invalid: Wrong HTTP URL",
    )
    await tap(dp, bot, Menu(go=Go.BUY, plan="month", provider="fake").pack())
    first, second = session.calls(EditMessageText)
    assert labels(first)[0] == "Оплатить 990 ₽"
    assert "Оплатить 990 ₽: http://localhost:8000/fake-checkout/" in second.text
    assert labels(second) == ["« Все тарифы"]


async def test_a_menu_message_that_cannot_be_edited_is_answered_with_a_new_one(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    session.add_error(EditMessageText, 400, "Bad Request: message can't be edited")
    await tap(dp, bot, Menu(go=Go.STATUS).pack())
    assert len(session.calls(EditMessageText)) == 1
    assert last_screen(session).text.startswith("У вас пока нет подписки.")


async def test_a_double_tap_on_the_same_button_is_not_an_error(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    session.add_error(
        EditMessageText,
        400,
        "Bad Request: message is not modified: specified new message "
        "content and reply markup are exactly the same",
    )
    await tap(dp, bot, Menu(go=Go.PLANS).pack())
    assert session.calls(SendMessage) == []


async def test_an_unexpected_failure_still_answers_the_button(
    dp: Dispatcher, bot: Bot, session: MockedSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise ConnectionError("database is gone")

    monkeypatch.setattr(club, "start_checkout", broken)
    await tap(dp, bot, Menu(go=Go.BUY, plan="month", provider="fake").pack())
    [answer] = session.calls(AnswerCallbackQuery)
    assert (answer.text, answer.show_alert) == (GENERIC_ERROR, True)
    assert session.calls(EditMessageText) == []


async def test_other_text_gets_the_command_list_and_groups_are_ignored(
    dp: Dispatcher, bot: Bot, session: MockedSession
) -> None:
    await feed(dp, bot, message_update(MEMBER, "привет"))
    assert last_screen(session).text.startswith("Команды: /start")
    await feed(dp, bot, message_update(MEMBER, "/start", chat_id=-100500, chat_type="supergroup"))
    assert len(session.calls(SendMessage)) == 1
