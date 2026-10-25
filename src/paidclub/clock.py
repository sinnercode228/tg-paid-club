"""Injectable clock. Everything that reads "now" goes through a :class:`Clock`, always in UTC.

Billing logic is all about dates (period ends, grace windows, retry schedules), so tests and the
``simulate`` command drive time with :class:`FakeClock` instead of sleeping or patching.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("FakeClock cannot move backwards")
        self._now += delta
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = ensure_utc(moment)


def ensure_utc(moment: datetime) -> datetime:
    """Return ``moment`` in UTC; naive datetimes are rejected rather than guessed."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"naive datetime is ambiguous, pass an aware one: {moment!r}")
    return moment.astimezone(UTC)
