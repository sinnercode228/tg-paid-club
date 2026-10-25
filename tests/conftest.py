from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from paidclub.billing.fake import FakeProvider
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from tests.helpers import ADMIN_ID, CHAT_ID, OWNER_ID, FakeRevoker, RecordingSender

#: The 31st on purpose: month arithmetic has to clamp (Feb 28) and then come back (Mar 31).
START = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)


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
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'club.db'}",
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
