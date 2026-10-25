from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import DeleteWebhook

from paidclub import cli, runner
from paidclub.config import Settings

pytestmark = pytest.mark.usefixtures("clean_env")

TOKEN = "123456:AAH-real-looking_token"


def configure(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(f"PAIDCLUB_{name.upper()}", value)


def complete(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    configure(
        monkeypatch,
        database_url=f"sqlite+aiosqlite:///{workdir / 'club.db'}",
        bot_token=TOKEN,
        owner_id="1",
        club_chat_ids="-1001",
        fake_webhook_secret="fixed",
        admin_token="admin-token",
    )


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Settings, bool]]:
    """Replaces the server with a recorder: these tests are about the command line only."""
    calls: list[tuple[Settings, bool]] = []

    async def fake_main(settings: Settings, *, with_bot: bool = True) -> None:
        calls.append((settings, with_bot))

    monkeypatch.setattr(runner, "main", fake_main)
    return calls


def test_check_reports_what_is_missing(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check"]) == cli.EXIT_CONFIG
    out = capsys.readouterr().out
    assert "error: PAIDCLUB_BOT_TOKEN is not set" in out
    assert "warning: PAIDCLUB_OWNER_ID is not set" in out
    assert "settings are valid" not in out


def test_check_passes_a_complete_configuration(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    complete(monkeypatch, clean_env)
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == "settings are valid"
    assert TOKEN not in out
    assert "admin-token" not in out


def test_invalid_settings_are_listed_by_variable_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, owner_id="me", club_chat_ids="-100x")
    assert cli.main(["run"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("Invalid settings")
    assert "  PAIDCLUB_OWNER_ID: " in err
    assert "  PAIDCLUB_CLUB_CHAT_IDS" in err
    assert "Traceback" not in err


def test_settings_come_from_the_env_file(
    clean_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (clean_env / ".env").write_text(f"PAIDCLUB_BOT_TOKEN={TOKEN}\nPAIDCLUB_OWNER_ID=42\n")
    assert cli.main(["check"]) == 0
    assert "owner 42" in capsys.readouterr().out


def test_run_refuses_to_start_without_a_bot_token(
    served: list[tuple[Settings, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run"]) == cli.EXIT_CONFIG
    assert "paidclub run cannot start" in capsys.readouterr().err
    assert served == []


def test_run_and_web_start_the_server(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path, served: list[tuple[Settings, bool]]
) -> None:
    complete(monkeypatch, clean_env)
    assert cli.main(["run"]) == 0
    assert cli.main(["web"]) == 0
    assert [(settings.bot_mode, with_bot) for settings, with_bot in served] == [
        ("polling", True),
        ("polling", False),
    ]


def test_web_starts_without_a_bot_token(served: list[tuple[Settings, bool]]) -> None:
    assert cli.main(["web"]) == 0
    assert [with_bot for _, with_bot in served] == [False]


def test_mode_flag_overrides_the_setting_and_is_validated(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: Path,
    served: list[tuple[Settings, bool]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    complete(monkeypatch, clean_env)
    assert cli.main(["run", "--mode", "webhook"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "PAIDCLUB_BOT_MODE=webhook needs an https:// PAIDCLUB_PUBLIC_BASE_URL" in err

    configure(monkeypatch, public_base_url="https://club.example", telegram_webhook_secret="s3cret")
    # The fake provider for a club on a public address has to be declared a demo.
    assert cli.main(["run", "--mode", "webhook"]) == cli.EXIT_CONFIG
    assert "PAIDCLUB_DEMO=1" in capsys.readouterr().err
    configure(monkeypatch, demo="1")
    assert cli.main(["run", "--mode", "webhook"]) == 0
    assert served[-1][0].bot_mode == "webhook"


def test_a_rejected_token_is_explained(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    complete(monkeypatch, clean_env)

    async def rejected(settings: Settings, *, with_bot: bool = True) -> None:
        raise TelegramUnauthorizedError(method=DeleteWebhook(), message="Unauthorized")

    monkeypatch.setattr(runner, "main", rejected)
    assert cli.main(["run"]) == cli.EXIT_FAILED
    assert "Telegram rejected PAIDCLUB_BOT_TOKEN" in capsys.readouterr().err


def test_a_crash_exits_with_status_1(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    complete(monkeypatch, clean_env)

    async def crash(settings: Settings, *, with_bot: bool = True) -> None:
        raise runner.ComponentStopped("the web server could not start on 127.0.0.1:8000")

    monkeypatch.setattr(runner, "main", crash)
    assert cli.main(["run"]) == cli.EXIT_FAILED
    assert "could not start on 127.0.0.1:8000" in caplog.text


def test_init_db_creates_the_tables(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = clean_env / "club.db"
    configure(monkeypatch, database_url=f"sqlite:///{path}")
    assert cli.main(["init-db"]) == 0
    assert f"sqlite+aiosqlite:///{path}" in capsys.readouterr().out
    with closing(sqlite3.connect(path)) as db:  # sqlite3's own context manager only commits
        tables = {row[0] for row in db.execute("select name from sqlite_master")}
    assert {"members", "subscriptions", "payments", "notifications"} <= tables


def test_simulate_is_wired_in(clean_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = clean_env / "demo.db"
    assert cli.main(["simulate", "--db", str(path), "--days", "3", "--members", "4"]) == 0
    assert path.exists()
    assert "Simulated 3 days" in capsys.readouterr().out


def test_every_command_is_listed_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for command in ("run", "web", "check", "init-db", "simulate"):
        assert command in out
