"""Shared fixtures.

Tests run on a fresh SQLite file each. With ``PAIDCLUB_TEST_DATABASE_URL`` set to a PostgreSQL
database whose name contains "test", the tests that use the ``database_url`` fixture run on
it instead (it is emptied before every test), so the ``FOR UPDATE SKIP LOCKED`` claims and
``ON CONFLICT`` inserts are exercised on the real thing::

    PAIDCLUB_TEST_DATABASE_URL=postgresql+asyncpg://paidclub:paidclub@localhost/paidclub_test
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.engine import make_url

from paidclub.billing.fake import FakeProvider
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.db import create_engine
from paidclub.models import Base
from tests.helpers import ADMIN_ID, CHAT_ID, OWNER_ID, FakeRevoker, RecordingSender

#: The 31st on purpose: month arithmetic has to clamp (Feb 28) and then come back (Mar 31).
START = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)

TEST_DATABASE_URL = os.environ.get("PAIDCLUB_TEST_DATABASE_URL") or None


def pytest_report_header() -> str:
    if TEST_DATABASE_URL is None:
        return "database: a SQLite file per test"
    return f"database: {make_url(TEST_DATABASE_URL).render_as_string(hide_password=True)}"


@pytest.fixture
async def database_url(tmp_path: Path) -> str:
    """An empty database with the schema: a new SQLite file, or the PostgreSQL test database
    with every table dropped and created again."""
    if TEST_DATABASE_URL is None:
        return f"sqlite+aiosqlite:///{tmp_path / 'club.db'}"
    url = make_url(TEST_DATABASE_URL)
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or ""):
        pytest.exit(
            "PAIDCLUB_TEST_DATABASE_URL must be a PostgreSQL database with 'test' in its name: "
            "every table in it is dropped before each test",
            returncode=2,
        )
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
    return TEST_DATABASE_URL


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No PAIDCLUB_* variable or .env file of the developer's leaks into the test; the test
    runs in an empty directory, which is returned."""
    for name in list(os.environ):
        if name.startswith("PAIDCLUB_"):
            monkeypatch.delenv(name)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        database_url=database_url,
        owner_id=OWNER_ID,
        admin_ids=[ADMIN_ID],
        club_chat_ids=[CHAT_ID],
        providers=["fake"],
        fake_webhook_secret="test-secret",  # type: ignore[arg-type]
        grace_days=3,
        dunning_retry_days=[1, 3],
        reminder_days=[3, 1],
        provider_retry_seconds=300,
        pending_recheck_seconds=900,
        claim_lease_seconds=300,
    )


@pytest.fixture
async def core(settings: Settings, clock: FakeClock) -> AsyncIterator[Core]:
    instance = await build_core(settings, clock=clock)
    yield instance
    await instance.aclose()


@pytest.fixture
def fake(core: Core) -> FakeProvider:
    return cast(FakeProvider, core.providers.require("fake"))


@pytest.fixture
def revoker() -> FakeRevoker:
    return FakeRevoker()


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()
