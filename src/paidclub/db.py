"""Engine and session factory, schema creation, and the two concurrency helpers the workers use.

* :func:`claim` hands rows to one worker at a time. On PostgreSQL candidates are read with
  ``SELECT ... FOR UPDATE SKIP LOCKED``; on every backend the claim itself is a conditional
  ``UPDATE`` that only succeeds on rows nobody holds, which is what makes it safe on SQLite.
* :func:`in_transaction` runs a unit of work and retries it on a fresh session when it lost a
  race (an optimistic-lock conflict or a unique-key collision with a concurrent writer).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, Table, event, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool

from paidclub.models import Base, ClaimMixin

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    if not url.startswith("sqlite"):
        return create_async_engine(url, echo=echo, pool_pre_ping=True)

    in_memory = ":memory:" in url or url.rstrip("/").endswith("sqlite+aiosqlite:")
    engine = create_async_engine(
        url,
        echo=echo,
        # Wait for a competing writer instead of failing with "database is locked".
        connect_args={"timeout": 30},
        poolclass=StaticPool if in_memory else None,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if not in_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def make_sessionmaker(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


def new_token() -> str:
    return uuid.uuid4().hex


async def claim(
    session: AsyncSession,
    model: type[ClaimMixin],
    *conditions: ColumnElement[bool],
    now: datetime,
    lease: timedelta,
    limit: int,
    order_by: Any = None,
    token: str | None = None,
) -> tuple[str, list[int]]:
    """Lease up to ``limit`` free rows matching ``conditions`` to this caller.

    Returns ``(token, ids)``; the caller commits, does the work, and clears or extends the
    lease with :func:`release`. A worker that dies keeps its rows only until the lease ends.
    """
    token = token or new_token()
    free = or_(model.claimed_until.is_(None), model.claimed_until <= now)
    candidates = select(model.id).where(*conditions, free).limit(limit)
    if order_by is not None:
        candidates = candidates.order_by(order_by)
    if dialect_name(session) == "postgresql":
        candidates = candidates.with_for_update(skip_locked=True)
    ids = list((await session.scalars(candidates)).all())
    if not ids:
        return token, []
    await session.execute(
        update(model)
        .where(model.id.in_(ids), *conditions, free)
        .values(claim_token=token, claimed_until=now + lease)
        .execution_options(synchronize_session=False)
    )
    mine = await session.scalars(
        select(model.id).where(model.id.in_(ids), model.claim_token == token)
    )
    return token, list(mine.all())


async def release(
    session: AsyncSession,
    model: type[ClaimMixin],
    row_id: int,
    token: str,
    *,
    until: datetime | None = None,
) -> bool:
    """Give a claimed row back. ``until`` keeps it untouchable for a while (a backoff)."""
    result = await session.execute(
        update(model)
        .where(model.id == row_id, model.claim_token == token)
        .values(claim_token=None, claimed_until=until)
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def insert_ignore(
    session: AsyncSession, table: Table, values: dict[str, Any], conflict_columns: list[str]
) -> bool:
    """``INSERT ... ON CONFLICT DO NOTHING``; returns whether a row was inserted."""
    name = dialect_name(session)
    if name == "postgresql":
        pg_stmt = postgresql.insert(table).values(**values)
        stmt: Any = pg_stmt.on_conflict_do_nothing(index_elements=conflict_columns)
    elif name == "sqlite":
        lite_stmt = sqlite.insert(table).values(**values)
        stmt = lite_stmt.on_conflict_do_nothing(index_elements=conflict_columns)
    else:  # pragma: no cover - only the two supported backends
        raise NotImplementedError(f"insert_ignore is not implemented for {name}")
    result = await session.execute(stmt)
    return cast(CursorResult[Any], result).rowcount == 1


class ConcurrentUpdateError(RuntimeError):
    """Still losing the race after several retries; let the caller (or provider) retry later."""


async def in_transaction[T](
    sessions: SessionFactory,
    work: Callable[[AsyncSession], Awaitable[T]],
    *,
    attempts: int = 4,
) -> T:
    """Run ``work`` in its own transaction; retry on a fresh session if it lost a race."""
    for attempt in range(1, attempts + 1):
        try:
            async with sessions() as session, session.begin():
                return await work(session)
        except (StaleDataError, IntegrityError) as exc:
            if attempt == attempts:
                raise ConcurrentUpdateError(str(exc)) from exc
            log.info(
                "concurrent update, retrying (%s/%s): %s", attempt, attempts, type(exc).__name__
            )
    raise AssertionError("unreachable")  # pragma: no cover
