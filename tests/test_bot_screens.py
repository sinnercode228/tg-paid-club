from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from paidclub.bot import screens
from paidclub.bot.screens import Go, Menu, Offer, PlanOffers
from paidclub.club import CheckoutLink, SubscriptionView
from paidclub.config import PlanConfig
from paidclub.domain.subscriptions import SubStatus
from paidclub.money import Money
from paidclub.notifications import Button, MessageDraft

MSK = ZoneInfo("Europe/Moscow")
END = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
RUB = Money(99000, "RUB")
LINKS = [("Клуб", "https://t.me/+club")]


def view(status: SubStatus, *, has_access: bool = True, **fields: object) -> SubscriptionView:
    values: dict[str, object] = {
        "id": 7,
        "plan_code": "month",
        "provider": "fake",
        "status": status,
        "price": RUB,
        "period_end": END,
        "grace_until": None,
        "next_charge_at": END,
        "has_saved_method": True,
        "has_access": has_access,
    }
    values.update(fields)
    return SubscriptionView(**values)  # type: ignore[arg-type]


def status(item: SubscriptionView, *, stars: bool = False) -> MessageDraft:
    return screens.status(
        item, plan_title="1 месяц", invite_links=LINKS, tz=MSK, provider_renewed=stars
    )


def labels(draft: MessageDraft) -> list[str]:
    return [button.text for button in draft.buttons]


@pytest.mark.parametrize(
    ("unit", "count", "period", "every"),
    [
        ("month", 1, "1 месяц", "каждый месяц"),
        ("month", 3, "3 месяца", "каждые 3 месяца"),
        ("month", 12, "12 месяцев", "каждые 12 месяцев"),
        ("month", 21, "21 месяц", "каждый 21 месяц"),
        ("day", 30, "30 дней", "каждые 30 дней"),
        ("day", 1, "1 день", "каждый день"),
    ],
)
def test_periods_read_naturally(unit: str, count: int, period: str, every: str) -> None:
    assert screens.period_text(unit, count) == period
    assert screens.every_text(unit, count) == every


def test_plans_without_a_way_to_pay_are_not_offered() -> None:
    # Only Stars is enabled: the month plan has a Stars price, the year plan has none.
    month = PlanConfig(code="month", title="1 месяц", price_rub=Decimal("990"), price_stars=500)
    year = PlanConfig(code="year", title="Год", period_count=12, price_rub=Decimal("8900"))
    draft = screens.plans(
        [PlanOffers(month, (Offer("stars", Money(500, "XTR")),)), PlanOffers(year, ())],
        intro="Привет.",
    )
    assert draft.text.startswith("Привет.\n\nТарифы:\n• 1 месяц — 500 ⭐\n\n")
    assert labels(draft) == ["1 месяц — 500 ⭐"]
    nothing = screens.plans([PlanOffers(year, ())], intro="Привет.")
    assert nothing == MessageDraft("Привет.\n\nОплата сейчас недоступна, загляните позже.")


def test_an_active_subscription_with_a_saved_card() -> None:
    draft = status(view(SubStatus.ACTIVE))
    assert draft.text.startswith(
        "Подписка «1 месяц» активна до 28.02.2026.\n"
        "Автопродление включено: 28.02.2026 спишем 990 ₽ с сохранённой карты.\n"
    )
    assert labels(draft) == ["Вступить в клуб", "Отключить автопродление"]
    assert draft.buttons[1].callback_data == Menu(go=Go.CANCEL, sub=7).pack()


def test_an_active_stars_subscription_is_renewed_by_telegram() -> None:
    draft = status(view(SubStatus.ACTIVE, price=Money(500, "XTR")), stars=True)
    assert "Автопродление включено: Telegram сам продлит подписку за 500 ⭐." in draft.text
    assert labels(draft) == ["Вступить в клуб", "Отключить автопродление"]


def test_an_active_subscription_without_a_saved_card_asks_to_pay() -> None:
    draft = status(view(SubStatus.ACTIVE, has_saved_method=False))
    assert "Автосписание не настроено: чтобы не потерять доступ, оплатите продление до " in (
        draft.text
    )
    assert labels(draft) == ["Вступить в клуб", "Оплатить продление 990 ₽", "Не продлевать"]


def test_past_due_without_grace_left_has_no_join_button() -> None:
    draft = status(view(SubStatus.PAST_DUE, has_access=False, grace_until=END, next_charge_at=None))
    assert draft.text == "Продление подписки «1 месяц» не оплачено, доступ к клубу закрыт."
    assert labels(draft) == ["Оплатить 990 ₽"]


def test_a_missing_stars_renewal_has_no_retry_line() -> None:
    draft = status(
        view(
            SubStatus.PAST_DUE,
            price=Money(500, "XTR"),
            grace_until=END + timedelta(days=3),
            next_charge_at=None,
        ),
        stars=True,
    )
    assert draft.text.startswith(
        "Telegram не продлил подписку «1 месяц» — возможно, на балансе не хватило Stars.\n"
        "Доступ к клубу сохранится до 03.03.2026.\n"
    )
    assert "Повторим" not in draft.text


def test_a_canceled_subscription_whose_period_ended_offers_the_plans() -> None:
    draft = status(view(SubStatus.CANCELED, has_access=False, next_charge_at=None))
    assert draft.text == ("Оплаченный период подписки «1 месяц» закончился, доступ к клубу закрыт.")
    assert [b.callback_data for b in draft.buttons] == [Menu(go=Go.PLANS).pack()]


def test_several_club_chats_get_one_join_button_each() -> None:
    draft = screens.status(
        view(SubStatus.ACTIVE),
        plan_title="1 месяц",
        invite_links=[("Канал", "https://t.me/+a"), ("Чат", "https://t.me/+b")],
        tz=MSK,
        provider_renewed=False,
        note="Готово.",
    )
    assert draft.text.startswith("Готово.\nПодписка")
    assert labels(draft)[:2] == ["Вступить: Канал", "Вступить: Чат"]


def test_checkout_for_the_next_period_links_back_to_the_status() -> None:
    link = CheckoutLink(7, "yookassa", RUB, "https://yoomoney.ru/checkout/payments/v2/x")
    draft = screens.checkout(link, plan_title="1 месяц", renewal=True)
    assert draft.text.startswith("К оплате: 990 ₽ — продление подписки «1 месяц».")
    pay = draft.buttons[0]
    assert (pay.text, pay.url) == ("Оплатить 990 ₽", "https://yoomoney.ru/checkout/payments/v2/x")
    assert draft.buttons[1].callback_data == Menu(go=Go.STATUS).pack()


def test_menu_callback_data_fits_telegram_limit_for_the_longest_plan_code() -> None:
    data = Menu(go=Go.BUY, plan="x" * 32, provider="yookassa", sub=2**31).pack()
    assert len(data.encode()) <= 64
    assert Menu.unpack(data).plan == "x" * 32


def test_link_buttons_can_move_into_the_text() -> None:
    draft = MessageDraft(
        "К оплате.",
        (Button("Оплатить", url="http://localhost:8000/p"), Button("Назад", callback_data="m")),
    )
    moved = screens.inline_urls(draft)
    assert moved.text == "К оплате.\n\nОплатить: http://localhost:8000/p"
    assert moved.buttons == (Button("Назад", callback_data="m"),)
