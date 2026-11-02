from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paidclub import callbacks, texts
from paidclub.domain.subscriptions import ExpireReason
from paidclub.money import Money
from paidclub.notifications import Button

MSK = ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (1, "день"),
        (2, "дня"),
        (4, "дня"),
        (5, "дней"),
        (11, "дней"),
        (12, "дней"),
        (21, "день"),
        (22, "дня"),
        (111, "дней"),
    ],
)
def test_russian_plurals(n: int, word: str) -> None:
    assert texts.plural(n, "день", "дня", "дней") == word


def test_when_phrase() -> None:
    assert texts.when_phrase(timedelta(days=1)) == "Завтра"
    assert texts.when_phrase(timedelta(days=3)) == "Через 3 дня"
    assert texts.when_phrase(timedelta(days=7)) == "Через 7 дней"
    assert texts.when_phrase(timedelta(hours=12)) == "Через 12 часов"


def test_dates_are_shown_in_the_display_timezone() -> None:
    # 22:30 UTC on the 28th is already the 1st in Moscow.
    assert texts.fmt_date(datetime(2026, 2, 28, 22, 30, tzinfo=UTC), MSK) == "01.03.2026"


def test_activation_message_links_every_club_chat() -> None:
    message = texts.activated(
        plan_title="1 месяц",
        period_end=datetime(2026, 2, 28, 9, 0, tzinfo=UTC),
        invite_links=[("Канал", "https://t.me/+a"), ("Чат", "https://t.me/+b")],
        tz=MSK,
    )
    assert message.text.startswith("Оплата прошла, подписка «1 месяц» активна до 28.02.2026.")
    assert message.buttons == (
        Button("Вступить: Канал", url="https://t.me/+a"),
        Button("Вступить: Чат", url="https://t.me/+b"),
    )


def test_activation_without_a_link_points_to_start() -> None:
    message = texts.activated(
        plan_title="1 месяц",
        period_end=datetime(2026, 2, 28, 9, 0, tzinfo=UTC),
        invite_links=[],
        tz=MSK,
        reactivated=True,
    )
    assert "снова открыт" in message.text
    assert "/start" in message.text
    assert message.buttons == ()


def test_payment_failed_offers_to_pay_now_and_says_until_when_access_lasts() -> None:
    message = texts.payment_failed(
        subscription_id=7,
        amount=Money(99000, "RUB"),
        grace_until=datetime(2026, 3, 3, 9, 0, tzinfo=UTC),
        next_retry_at=datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
        tz=MSK,
    )
    assert "Не получилось списать 990 ₽" in message.text
    assert "сохранится до 03.03.2026" in message.text
    assert "Повторим попытку 01.03.2026" in message.text
    assert message.buttons == (Button("Оплатить 990 ₽", callback_data="pay:7"),)

    last = texts.payment_failed(
        subscription_id=7,
        amount=Money(99000, "RUB"),
        grace_until=datetime(2026, 3, 3, 9, 0, tzinfo=UTC),
        next_retry_at=None,
        tz=MSK,
    )
    assert "Автоматических попыток больше не будет" in last.text


def test_crypto_members_are_asked_to_pay_never_told_about_a_card() -> None:
    reminder = texts.reminder_crypto(
        subscription_id=7,
        amount=Money(1000, "USD"),
        period_end=datetime(2026, 2, 28, 9, 0, tzinfo=UTC),
        offset=timedelta(days=3),
        tz=MSK,
    )
    assert reminder.text == (
        "Через 3 дня, 28.02.2026, заканчивается оплаченный период. При оплате криптовалютой "
        "подписка сама не продлевается: оплатите следующий период, чтобы не потерять доступ. "
        "Можно уже сейчас — он начнётся 28.02.2026, ни один день не пропадёт."
    )
    assert reminder.buttons == (Button("Оплатить 10 $", callback_data="pay:7"),)

    unpaid = texts.crypto_renewal_unpaid(
        subscription_id=7,
        amount=Money(1000, "USD"),
        grace_until=datetime(2026, 3, 3, 9, 0, tzinfo=UTC),
        tz=MSK,
    )
    assert unpaid.text.startswith("Оплаченный период закончился, а следующий не оплачен")
    assert "сохранится до 03.03.2026" in unpaid.text
    assert unpaid.buttons == (Button("Оплатить 10 $", callback_data="pay:7"),)
    for message in (reminder, unpaid):
        assert "карт" not in message.text
        assert "Повторим" not in message.text
        assert "списа" not in message.text


def test_access_ended_offers_the_plans() -> None:
    message = texts.access_ended(reason=ExpireReason.CANCELED)
    assert message.buttons == (Button("Выбрать тариф", callback_data=callbacks.PLANS),)


def test_callbacks_round_trip() -> None:
    assert callbacks.parse(callbacks.pay(12)) == callbacks.Callback("pay", 12)
    assert callbacks.parse(callbacks.cancel(3)) == callbacks.Callback("cancel", 3)
    assert callbacks.parse(callbacks.resume(4)) == callbacks.Callback("resume", 4)
    assert callbacks.parse(callbacks.PLANS) == callbacks.Callback("plans")
    assert callbacks.parse("pay:x") is None
    assert callbacks.parse("drop:1") is None


def test_buttons_need_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Button("x")
    with pytest.raises(ValueError, match="exactly one"):
        Button("x", url="https://t.me", callback_data="plans")
    with pytest.raises(ValueError, match="64 bytes"):
        Button("x", callback_data="я" * 40)
