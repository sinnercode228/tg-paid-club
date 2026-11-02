"""Wiring: one :class:`Core` holds what every entry point (bot, web app, scheduler, CLI) shares.

Entry points build it once with :func:`build_core` and register the providers that need
objects only they have (the Stars provider needs the aiogram ``Bot``)::

    core = await build_core(settings)
    core.providers.register(StarsProvider(bot, stars_payments(core, secret=...)))
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine

from paidclub.billing import ProviderRegistry
from paidclub.billing.fake import FakeProvider
from paidclub.clock import Clock, SystemClock
from paidclub.config import Settings
from paidclub.db import SessionFactory, create_all, create_engine, make_sessionmaker
from paidclub.domain.subscriptions import BillingPolicy


@dataclass
class Core:
    settings: Settings
    engine: AsyncEngine
    sessions: SessionFactory
    clock: Clock
    providers: ProviderRegistry
    policy: BillingPolicy
    tz: ZoneInfo

    async def aclose(self) -> None:
        await self.engine.dispose()


def default_providers(settings: Settings, clock: Clock) -> ProviderRegistry:
    """Providers that need nothing but settings. YooKassa and Stars are registered by the
    entry points that own an HTTP client / a bot."""
    registry = ProviderRegistry()
    if "fake" in settings.providers:
        registry.register(
            FakeProvider(
                secret=settings.fake_webhook_secret.get_secret_value(),
                base_url=settings.public_base_url,
                clock=clock,
            )
        )
    return registry


async def build_core(
    settings: Settings,
    *,
    clock: Clock | None = None,
    providers: ProviderRegistry | None = None,
    engine: AsyncEngine | None = None,
    create_schema: bool | None = None,
) -> Core:
    """Create the engine, session factory and provider registry.

    The schema is created automatically on SQLite (dev, tests, the demo); on PostgreSQL pass
    ``create_schema=True`` explicitly (``paidclub init-db`` does).
    """
    clock = clock or SystemClock()
    engine = engine or create_engine(settings.database_url)
    if create_schema is None:
        create_schema = engine.dialect.name == "sqlite"
    if create_schema:
        await create_all(engine)
    return Core(
        settings=settings,
        engine=engine,
        sessions=make_sessionmaker(engine),
        clock=clock,
        providers=providers if providers is not None else default_providers(settings, clock),
        policy=settings.billing_policy(),
        tz=ZoneInfo(settings.display_timezone),
    )
