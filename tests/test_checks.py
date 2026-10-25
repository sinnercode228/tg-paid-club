from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from paidclub.checks import (
    ConfigError,
    describe,
    load_settings,
    safe_database_url,
    startup_problems,
)
from paidclub.config import DEFAULT_PLANS, PlanConfig, Settings

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456:AAH-real-looking_token"

pytestmark = pytest.mark.usefixtures("clean_env")


def make(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def errors_of(monkeypatch: pytest.MonkeyPatch, **env: str) -> list[str]:
    for name, value in env.items():
        monkeypatch.setenv(f"PAIDCLUB_{name.upper()}", value)
    with pytest.raises(ConfigError) as caught:
        load_settings()
    return caught.value.lines


def test_errors_name_the_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = errors_of(monkeypatch, owner_id="me", web_port="99999", log_level="loud")
    assert sorted(line.split(":")[0] for line in lines) == [
        "PAIDCLUB_LOG_LEVEL",
        "PAIDCLUB_OWNER_ID",
        "PAIDCLUB_WEB_PORT",
    ]


def test_errors_between_settings_name_both_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    [line] = errors_of(monkeypatch, providers="yookassa")
    assert line == (
        "the yookassa provider needs PAIDCLUB_YOOKASSA_SHOP_ID and PAIDCLUB_YOOKASSA_SECRET_KEY"
    )
    [line] = errors_of(monkeypatch, providers="fake", grace_days="2")
    assert "PAIDCLUB_GRACE_DAYS=2.0" in line


def test_errors_never_echo_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = errors_of(
        monkeypatch,
        bot_token="123456 secret with spaces",
        database_url="mysql://root:hunter2@db/club",
        telegram_webhook_secret="has spaces and ünicode",
    )
    text = "\n".join(lines)
    assert "secret with spaces" not in text
    assert "hunter2" not in text
    assert "ünicode" not in text
    assert {line.split(":")[0] for line in lines} == {
        "PAIDCLUB_BOT_TOKEN",
        "PAIDCLUB_DATABASE_URL",
        "PAIDCLUB_TELEGRAM_WEBHOOK_SECRET",
    }


def test_nested_plan_errors_point_at_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    [line] = errors_of(monkeypatch, plans='[{"code": "Month", "title": "M", "price_rub": "1"}]')
    assert line.startswith("PAIDCLUB_PLANS[0].code: ")
    [line] = errors_of(monkeypatch, plans='[{"code": "m", "title": "M"')
    assert line == "PAIDCLUB_PLANS: not valid JSON"


def test_empty_values_in_env_mean_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    (Path.cwd() / ".env").write_text(
        "PAIDCLUB_OWNER_ID=\nPAIDCLUB_BOT_TOKEN=\nPAIDCLUB_CHECKOUT_RETURN_URL=\n"
    )
    settings = load_settings()
    assert settings.owner_id is None
    assert settings.bot_token is None
    assert settings.checkout_return_url is None


def test_urls_are_normalised() -> None:
    settings = make(
        public_base_url=" https://club.example/ ",
        database_url="postgres://club:pw@db:5432/club",
        checkout_return_url="https://t.me/club_bot?start=paid",
    )
    assert settings.public_base_url == "https://club.example"
    assert settings.telegram_webhook_url == "https://club.example/telegram/webhook"
    assert settings.database_url == "postgresql+asyncpg://club:pw@db:5432/club"
    assert settings.checkout_return_url == "https://t.me/club_bot?start=paid"
    assert make(database_url="sqlite:///./x.db").database_url == "sqlite+aiosqlite:///./x.db"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"public_base_url": "club.example"}, "http:// or https://"),
        ({"public_base_url": "https://club.example/?x=1"}, "query string"),
        ({"checkout_return_url": "t.me/club_bot"}, "http:// or https://"),
        ({"display_timezone": "Moscow"}, "IANA name"),
        ({"bot_mode": "webhook", "telegram_webhook_secret": "s3cret"}, "only delivers webhooks"),
        ({"bot_mode": "webhook", "public_base_url": "https://c.example"}, "needs telegram_"),
    ],
)
def test_invalid_values_are_rejected(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make(**values)


def test_run_needs_a_bot_token_and_warns_about_the_rest() -> None:
    problems = startup_problems(make(), "run")
    assert [p.message.split(" ")[0] for p in problems if p.severity == "error"] == [
        "PAIDCLUB_BOT_TOKEN"
    ]
    warnings = " ".join(p.message for p in problems if p.severity == "warning")
    for name in ("PAIDCLUB_OWNER_ID", "PAIDCLUB_CLUB_CHAT_IDS", "PAIDCLUB_FAKE_WEBHOOK_SECRET"):
        assert name in warnings
    assert "/admin dashboard is off" in warnings


def test_a_complete_configuration_has_no_problems() -> None:
    settings = make(
        bot_token=TOKEN,
        owner_id=1,
        club_chat_ids=[-1001],
        fake_webhook_secret="fixed",
        admin_token="t",
    )
    assert startup_problems(settings, "run") == []


def test_web_only_does_not_need_the_bot() -> None:
    problems = startup_problems(make(fake_webhook_secret="fixed", admin_password="pw"), "web")
    assert problems == []


def test_webhook_mode_needs_a_port_telegram_delivers_to() -> None:
    settings = make(
        bot_token=TOKEN,
        bot_mode="webhook",
        telegram_webhook_secret="s3cret",
        public_base_url="https://club.example:8000",
    )
    [error] = [p for p in startup_problems(settings, "run") if p.severity == "error"]
    assert "port 8000" in error.message
    on_443 = settings.model_copy(update={"public_base_url": "https://club.example"})
    assert not [p for p in startup_problems(on_443, "run") if p.severity == "error"]


def test_yookassa_without_https_is_warned_about() -> None:
    settings = make(
        providers=["yookassa"], yookassa_shop_id="1", yookassa_secret_key="k", admin_token="t"
    )
    [warning] = startup_problems(settings, "web")
    assert "/webhooks/yookassa" in warning.message


def test_describe_hides_secrets() -> None:
    settings = make(
        bot_token=TOKEN,
        admin_token="admin-secret",
        database_url="postgresql+asyncpg://club:db-password@db/club",
    )
    text = "\n".join(describe(settings))
    assert "db-password" not in text
    assert TOKEN not in text
    assert "admin-secret" not in text
    assert "postgresql+asyncpg://club:***@db/club" in text
    assert "/admin bearer token" in text
    assert safe_database_url("sqlite+aiosqlite:///./x.db") == "sqlite+aiosqlite:///./x.db"


ENV_LINE = re.compile(r"^#?\s*(PAIDCLUB_[A-Z0-9_]+)=", re.MULTILINE)


def test_env_example_documents_every_setting_and_nothing_else() -> None:
    documented = ENV_LINE.findall((ROOT / ".env.example").read_text(encoding="utf-8"))
    expected = {f"PAIDCLUB_{name.upper()}" for name in Settings.model_fields}
    assert len(documented) == len(set(documented)), "a variable is listed twice"
    assert set(documented) == expected


def test_env_example_loads_as_valid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    (Path.cwd() / ".env").write_text(
        (ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    settings = load_settings()
    assert settings.providers == ["fake"]
    assert [plan.code for plan in settings.plans] == ["month", "quarter", "year"]


def test_env_example_shows_the_default_plans() -> None:
    [line] = [
        line
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.startswith("# PAIDCLUB_PLANS=")
    ]
    plans = json.loads(line.removeprefix("# PAIDCLUB_PLANS=").strip("'"))
    assert tuple(PlanConfig.model_validate(plan) for plan in plans) == DEFAULT_PLANS
