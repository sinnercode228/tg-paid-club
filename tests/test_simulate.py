"""The ``simulate`` command: realistic data through the real code paths, reproducible, and
never written over a database that already has members."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from paidclub.config import Settings
from paidclub.db import create_engine, make_sessionmaker
from paidclub.domain.subscriptions import LIVE_STATUSES
from paidclub.models import Notification, NotificationStatus, Payment, PaymentStatus, Subscription
from paidclub.simulate import (
    Behaviour,
    SimulationConfig,
    SimulationError,
    SimulationResult,
    configure_parser,
    default_start,
    run_cli,
    simulate,
)
from paidclub.stats import report_to_dict

START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
#: Busier members than the defaults, so a short run shows every kind of event.
BUSY = Behaviour(declining_card=0.25, no_saved_card=0.15, cancel_per_day=0.02)


def config(**overrides: object) -> SimulationConfig:
    values: dict[str, object] = {
        "days": 50,
        "members": 40,
        "seed": 11,
        "start": START,
        "tick": timedelta(hours=6),
        "behaviour": BUSY,
    }
    values.update(overrides)
    return SimulationConfig(**values)  # type: ignore[arg-type]


def url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def counters(result: SimulationResult) -> dict[str, object]:
    data = asdict(result)
    data.pop("report")
    return data


async def test_simulation_fills_the_club(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "sim.db"
    result = await simulate(settings, config(), database_url=url(path))

    assert result.start == START
    assert result.end == START + timedelta(days=50)
    assert result.arrived == 40
    assert result.subscribed >= 30
    assert result.cancels > 0
    assert result.ticks == 50 * 4 + 1  # every 6 hours, plus the final one at the end
    report = result.report
    assert report is not None
    totals = report.totals
    assert totals.new > 0
    assert totals.renewals > 0
    assert totals.failed_charges > 0
    assert totals.dunning_started > 0
    assert totals.churned > 0
    assert totals.cancellations > 0

    engine = create_engine(url(path))
    try:
        async with make_sessionmaker(engine)() as session:
            live = await session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status.in_(LIVE_STATUSES))
            )
            revenue = await session.scalar(
                select(func.sum(Payment.amount_minor)).where(
                    Payment.status == PaymentStatus.SUCCEEDED,
                    Payment.applied.is_(True),
                    Payment.created_at >= report.start,
                )
            )
            unsent = await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.status == NotificationStatus.PENDING)
            )
    finally:
        await engine.dispose()

    # The report agrees with the tables it was built from.
    assert report.snapshot.total.live == live
    assert report.days[-1].live == live
    assert totals.revenue_minor == {"RUB": revenue}
    assert totals.new == totals.first_payments
    # Every message the run produced went through the outbox.
    assert unsent == 0
    assert result.messages_sent > 0


async def test_same_seed_same_history(settings: Settings, tmp_path: Path) -> None:
    short = config(days=30, members=25)
    first = await simulate(settings, short, database_url=url(tmp_path / "a.db"))
    second = await simulate(settings, short, database_url=url(tmp_path / "b.db"))
    other = await simulate(
        settings, config(days=30, members=25, seed=12), database_url=url(tmp_path / "c.db")
    )

    assert first.report is not None and second.report is not None and other.report is not None
    assert counters(first) == counters(second)
    assert report_to_dict(first.report) == report_to_dict(second.report)
    assert report_to_dict(first.report) != report_to_dict(other.report)


async def test_refuses_a_database_with_members(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "club.db"
    await simulate(settings, config(days=2, members=3), database_url=url(path))
    with pytest.raises(SimulationError, match="already has members"):
        await simulate(settings, config(days=2, members=3), database_url=url(path))


async def test_refuses_anything_but_sqlite(settings: Settings) -> None:
    with pytest.raises(SimulationError, match="SQLite"):
        await simulate(settings, config(), database_url="postgresql+asyncpg://club@db/club")


@pytest.mark.parametrize(
    "overrides",
    [{"days": 0}, {"members": -1}, {"tick": timedelta(0)}, {"renewal_failure_rate": 1.5}],
)
def test_rejects_nonsense_settings(overrides: dict[str, object]) -> None:
    with pytest.raises(SimulationError):
        config(**overrides)


def test_default_period_ends_at_the_current_hour() -> None:
    now = datetime(2026, 10, 25, 14, 37, 12, tzinfo=UTC)
    start = default_start(120, now=now)
    assert start == datetime(2026, 6, 27, 14, 0, tzinfo=UTC)
    assert start + timedelta(days=120) == now.replace(minute=0, second=0)


def test_command_line(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "demo.db"
    parser = configure_parser(argparse.ArgumentParser())
    argv = ["--db", str(path), "--days", "3", "--members", "4", "--seed", "2"]

    assert run_cli(parser.parse_args(argv), settings) == 0
    output = capsys.readouterr().out
    assert "Simulated 3 days" in output
    assert "members: 4 arrived" in output
    assert f"sqlite+aiosqlite:///{path}" in output

    with pytest.raises(SystemExit, match="already exists"):
        run_cli(parser.parse_args(argv), settings)
    assert run_cli(parser.parse_args([*argv, "--reset"]), settings) == 0
