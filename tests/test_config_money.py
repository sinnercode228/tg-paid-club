from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from paidclub.config import PlanConfig, Settings
from paidclub.money import Money


def make(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# --- money -------------------------------------------------------------------------------------


def test_money_round_trips_decimal_strings() -> None:
    assert Money.from_decimal("990.00", "RUB") == Money(99000, "RUB")
    assert Money.from_decimal("2490", "RUB").to_decimal_str() == "2490.00"
    assert Money(500, "XTR").to_decimal_str() == "500"
    assert Money.from_decimal("10", "USD") == Money(1000, "USD")
    assert Money(999, "USD").to_decimal_str() == "9.99"


def test_money_refuses_more_precision_than_the_currency_has() -> None:
    with pytest.raises(ValueError, match="decimal places"):
        Money.from_decimal("9.999", "RUB")
    with pytest.raises(ValueError, match="decimal places"):
        Money.from_decimal("1.5", "XTR")
    with pytest.raises(ValueError, match="decimal places"):
        Money.from_decimal("9.995", "USD")


def test_money_refuses_unknown_currencies_and_negative_amounts() -> None:
    with pytest.raises(ValueError, match="currency"):
        Money(100, "EUR")
    with pytest.raises(ValueError, match="negative"):
        Money(-1, "RUB")


def test_money_human_form() -> None:
    assert Money(99000, "RUB").human() == "990 ₽"
    assert Money(890000, "RUB").human() == "8 900 ₽"
    assert Money(149050, "RUB").human() == "1 490,50 ₽"
    assert Money(500, "XTR").human() == "500 ⭐"
    assert Money(1000, "USD").human() == "10 $"
    assert Money(999, "USD").human() == "9,99 $"


# --- settings ----------------------------------------------------------------------------------


def test_defaults_describe_three_plans_and_the_fake_provider() -> None:
    settings = make()
    assert [plan.code for plan in settings.plans] == ["month", "quarter", "year"]
    assert settings.providers == ["fake"]
    month = settings.plan("month")
    assert month is not None
    assert month.price_for("RUB") == Money(99000, "RUB")
    assert month.price_for("XTR") == Money(500, "XTR")
    assert month.price_for("USD") == Money(1000, "USD")
    assert month.currencies == ["RUB", "XTR", "USD"]
    quarter = settings.plan("quarter")
    assert quarter is not None
    assert quarter.price_for("XTR") is None
    assert quarter.currencies == ["RUB", "USD"]


def test_list_settings_accept_comma_separated_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIDCLUB_CLUB_CHAT_IDS", "-1001, -1002")
    monkeypatch.setenv("PAIDCLUB_ADMIN_IDS", "[7, 8]")
    monkeypatch.setenv("PAIDCLUB_DUNNING_RETRY_DAYS", "1,2")
    monkeypatch.setenv(
        "PAIDCLUB_PLANS",
        '[{"code": "week", "title": "Неделя", "period_unit": "day", '
        '"period_count": 7, "price_rub": "290.00"}]',
    )
    settings = make()
    assert settings.club_chat_ids == [-1001, -1002]
    assert settings.admin_ids == [7, 8]
    assert settings.dunning_retry_days == [1.0, 2.0]
    assert settings.plans[0].code == "week"


def test_crypto_pay_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIDCLUB_PROVIDERS", "cryptopay,stars")
    monkeypatch.setenv("PAIDCLUB_CRYPTOPAY_API_TOKEN", "12345:AAtoken")
    monkeypatch.setenv("PAIDCLUB_CRYPTOPAY_ASSETS", "usdt, ton ,btc")
    settings = make()
    assert settings.providers == ["cryptopay", "stars"]
    assert settings.cryptopay_assets == ["USDT", "TON", "BTC"]
    assert settings.cryptopay_api_url == "https://pay.crypt.bot/api"
    assert settings.cryptopay_invoice_lifetime_minutes == 60


def test_ton_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PAIDCLUB_TON_WALLET_ADDRESS",
        " 0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE ",
    )
    monkeypatch.setenv("PAIDCLUB_TON_API_KEY", "")  # empty means not set
    settings = make()
    assert settings.ton_wallet_address == (
        "0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE"
    )
    assert settings.ton_api_key is None
    assert settings.ton_jetton_master == "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
    assert (settings.ton_jetton_decimals, settings.ton_poll_seconds) == (6, 15)


def test_billing_policy_from_settings() -> None:
    policy = make(grace_days=5, dunning_retry_days=[1, 2, 5], reminder_days=[1, 7]).billing_policy()
    assert policy.grace == timedelta(days=5)
    assert policy.retry_offsets == (timedelta(days=1), timedelta(days=2), timedelta(days=5))
    assert policy.reminder_offsets == (timedelta(days=7), timedelta(days=1))


def test_protected_users_are_the_owner_and_admins() -> None:
    assert make(owner_id=1, admin_ids=[2, 3]).protected_user_ids == {1, 2, 3}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"grace_days": 2, "dunning_retry_days": [1, 3]}, "inside the grace period"),
        ({"dunning_retry_days": [3, 1]}, "ascending"),
        ({"reminder_days": [0]}, "positive"),
        ({"providers": []}, "at least one billing provider"),
        ({"providers": ["yookassa"]}, "yookassa_shop_id"),
        ({"env": "prod"}, "disable it in prod"),
        (
            {"providers": ["stars"], "plans": [{"code": "m", "title": "M", "price_rub": "1.00"}]},
            "price_stars",
        ),
        ({"providers": ["cryptopay"]}, "cryptopay_api_token"),
        (
            {
                "providers": ["cryptopay"],
                "cryptopay_api_token": "1:x",
                "plans": [{"code": "m", "title": "M", "price_rub": "1.00"}],
            },
            "price_usd",
        ),
        ({"cryptopay_assets": "USDT,USD T"}, "coin codes"),
        ({"cryptopay_assets": "[]"}, "at least one coin"),
        ({"cryptopay_assets": "TON,ton"}, "must not repeat"),
        ({"cryptopay_invoice_lifetime_minutes": 1441}, "less than or equal to 1440"),
        ({"ton_wallet_address": "my wallet"}, "must be a TON address"),
        ({"ton_jetton_master": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDt"}, "TON address"),
        ({"ton_jetton_decimals": 1}, "greater than or equal to 2"),
        ({"ton_invoice_lifetime_minutes": 4}, "greater than or equal to 5"),
    ],
)
def test_inconsistent_settings_are_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make(**overrides)


def test_stars_price_only_on_thirty_day_plans() -> None:
    PlanConfig(code="m", title="M", period_unit="day", period_count=30, price_stars=100)
    with pytest.raises(ValidationError, match="30 days"):
        PlanConfig(code="q", title="Q", period_count=3, price_stars=100)


def test_plan_needs_a_price_with_at_most_two_decimals() -> None:
    with pytest.raises(ValidationError, match="needs price_rub, price_stars, price_usd"):
        PlanConfig(code="x", title="X")
    with pytest.raises(ValidationError, match="decimal places"):
        PlanConfig(code="x", title="X", price_rub=Decimal("9.999"))
    with pytest.raises(ValidationError, match="decimal places"):
        PlanConfig(code="x", title="X", price_usd=Decimal("9.999"))
    with pytest.raises(ValidationError, match="greater than 0"):
        PlanConfig(code="x", title="X", price_usd=Decimal("0"))


def test_a_plan_can_be_priced_in_dollars_alone() -> None:
    plan = PlanConfig(code="vip", title="VIP", price_usd=Decimal("49.90"))
    assert plan.price_for("USD") == Money(4990, "USD")
    assert plan.price_for("RUB") is None
    assert plan.currencies == ["USD"]
