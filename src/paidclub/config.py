"""Settings, loaded from environment variables (prefix ``PAIDCLUB_``) or ``.env``.

List settings accept either JSON (``[1, 3]``) or a comma-separated string (``1,3``), so a
``.env`` line like ``PAIDCLUB_CLUB_CHAT_IDS=-1001234567890`` works as expected. Plans are JSON.
"""

from __future__ import annotations

import json
import secrets
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from paidclub.domain.subscriptions import BillingPolicy, PeriodUnit
from paidclub.money import Money

ProviderName = Literal["fake", "yookassa", "stars"]

#: Telegram Stars subscriptions renew every 30 days; it is the only period the Bot API accepts.
STARS_SUBSCRIPTION_PERIOD = timedelta(days=30)


class PlanConfig(BaseModel):
    """One plan the owner sells. Prices are snapshotted onto a subscription when it is created."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    title: str = Field(min_length=1, max_length=64)
    period_unit: PeriodUnit = "month"
    period_count: int = Field(default=1, ge=1, le=36)
    #: Price in rubles for card/fake providers, e.g. ``"990.00"``.
    price_rub: Decimal | None = Field(default=None, gt=0)
    #: Price in Telegram Stars per 30 days. Only for monthly (or 30-day) plans.
    price_stars: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_prices(self) -> Self:
        if self.price_rub is None and self.price_stars is None:
            raise ValueError(f"plan {self.code!r} needs price_rub, price_stars or both")
        if self.price_rub is not None:
            Money.from_decimal(self.price_rub, "RUB")  # rejects more than two decimal places
        if self.price_stars is not None and not self.is_stars_compatible:
            raise ValueError(
                f"plan {self.code!r}: Telegram Stars subscriptions renew every 30 days, "
                "so price_stars is only allowed on 1-month or 30-day plans"
            )
        return self

    @property
    def is_stars_compatible(self) -> bool:
        return (self.period_unit, self.period_count) in {("month", 1), ("day", 30)}

    def price_for(self, currency: str) -> Money | None:
        if currency == "RUB" and self.price_rub is not None:
            return Money.from_decimal(self.price_rub, "RUB")
        if currency == "XTR" and self.price_stars is not None:
            return Money(self.price_stars, "XTR")
        return None

    @property
    def months_equivalent(self) -> Decimal:
        """Length in months, used to normalise prices to MRR (30 days count as one month)."""
        if self.period_unit == "month":
            return Decimal(self.period_count)
        return Decimal(self.period_count) / Decimal(30)


DEFAULT_PLANS: tuple[PlanConfig, ...] = (
    PlanConfig(
        code="month", title="1 месяц", period_count=1, price_rub=Decimal("990.00"), price_stars=500
    ),
    PlanConfig(code="quarter", title="3 месяца", period_count=3, price_rub=Decimal("2490.00")),
    PlanConfig(code="year", title="12 месяцев", period_count=12, price_rub=Decimal("8900.00")),
)


def _only_fake() -> list[ProviderName]:
    return ["fake"]


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAIDCLUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- runtime ---------------------------------------------------------------------------
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    #: Timezone used to print dates in messages to members. Storage is always UTC.
    display_timezone: str = "Europe/Moscow"

    # --- storage ---------------------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./paidclub.db"

    # --- telegram --------------------------------------------------------------------------
    bot_token: SecretStr | None = None
    #: Telegram user id of the club owner: admin commands, alerts, never removed from chats.
    owner_id: int | None = None
    #: Other user ids that must never be removed from club chats (moderators, co-authors).
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    #: Private channel and/or group ids the subscription grants access to (``-100...``).
    club_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- web -------------------------------------------------------------------------------
    #: Public base URL of the FastAPI app (checkout pages, webhooks, return URLs).
    public_base_url: str = "http://localhost:8000"
    #: Bearer token for ``/admin``. Either this or ``admin_password`` must be set to open it.
    admin_token: SecretStr | None = None
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    #: Where providers send the member after paying, e.g. ``https://t.me/<your_bot>``.
    checkout_return_url: str | None = None

    # --- plans and billing -----------------------------------------------------------------
    plans: list[PlanConfig] = Field(default_factory=lambda: list(DEFAULT_PLANS))
    providers: Annotated[list[ProviderName], NoDecode] = Field(default_factory=_only_fake)
    #: Access is kept this long after a failed renewal charge while retries run.
    grace_days: float = Field(default=3, ge=0)
    #: Retry a failed renewal this many days after the first failure.
    dunning_retry_days: Annotated[list[float], NoDecode] = Field(default_factory=lambda: [1.0, 3.0])
    #: Remind members this many days before a renewal or the end of access.
    reminder_days: Annotated[list[float], NoDecode] = Field(default_factory=lambda: [3.0, 1.0])
    #: Stars renewals are charged by Telegram; wait this long for the payment update before
    #: treating the renewal as failed.
    stars_renewal_wait_minutes: float = Field(default=60, ge=0)

    # --- fake provider ---------------------------------------------------------------------
    #: Signs fake checkout tokens and webhooks. Set it explicitly when running several web
    #: workers, otherwise each process makes up its own.
    fake_webhook_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(secrets.token_urlsafe(32))
    )

    # --- yookassa --------------------------------------------------------------------------
    yookassa_shop_id: str | None = None
    yookassa_secret_key: SecretStr | None = None
    yookassa_api_url: str = "https://api.yookassa.ru/v3"

    # --- workers ---------------------------------------------------------------------------
    scheduler_interval_seconds: float = Field(default=30, gt=0)
    #: How long a worker owns a claimed renewal or notification before another may take it.
    claim_lease_seconds: float = Field(default=300, gt=0)
    #: When a provider call errors out (network, 5xx), retry the same charge after this long.
    provider_retry_seconds: float = Field(default=300, gt=0)
    #: A renewal charge the provider reports as pending is re-checked after this long.
    pending_recheck_seconds: float = Field(default=900, gt=0)
    outbox_batch_size: int = Field(default=20, ge=1)
    outbox_max_attempts: int = Field(default=8, ge=1)

    @field_validator(
        "admin_ids",
        "club_chat_ids",
        "providers",
        "dunning_retry_days",
        "reminder_days",
        mode="before",
    )
    @classmethod
    def _parse_lists(cls, value: Any) -> Any:
        return _split_csv(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if any(day <= 0 for day in self.dunning_retry_days):
            raise ValueError("dunning_retry_days must be positive")
        if self.dunning_retry_days != sorted(self.dunning_retry_days):
            raise ValueError("dunning_retry_days must be in ascending order")
        if self.dunning_retry_days and max(self.dunning_retry_days) > self.grace_days:
            raise ValueError(
                "the last dunning retry must fall inside the grace period "
                f"({max(self.dunning_retry_days)} > grace_days={self.grace_days})"
            )
        if any(day <= 0 for day in self.reminder_days):
            raise ValueError("reminder_days must be positive")
        codes = [plan.code for plan in self.plans]
        if len(codes) != len(set(codes)):
            raise ValueError("plan codes must be unique")
        if not self.plans:
            raise ValueError("at least one plan is required")
        if not self.providers:
            raise ValueError("at least one billing provider must be enabled")
        if len(self.providers) != len(set(self.providers)):
            raise ValueError("providers must not repeat")
        if "stars" in self.providers and not any(p.price_stars for p in self.plans):
            raise ValueError("the stars provider needs at least one plan with price_stars")
        if "yookassa" in self.providers and not (
            self.yookassa_shop_id and self.yookassa_secret_key
        ):
            raise ValueError("the yookassa provider needs yookassa_shop_id and yookassa_secret_key")
        if self.env == "prod" and "fake" in self.providers:
            raise ValueError("the fake provider is for demos and tests; disable it in prod")
        return self

    # --- derived values --------------------------------------------------------------------
    def plan(self, code: str) -> PlanConfig | None:
        return next((plan for plan in self.plans if plan.code == code), None)

    @property
    def protected_user_ids(self) -> frozenset[int]:
        """Users the bot never removes from a club chat."""
        ids = set(self.admin_ids)
        if self.owner_id is not None:
            ids.add(self.owner_id)
        return frozenset(ids)

    def billing_policy(self) -> BillingPolicy:
        return BillingPolicy(
            grace=timedelta(days=self.grace_days),
            retry_offsets=tuple(timedelta(days=day) for day in self.dunning_retry_days),
            reminder_offsets=tuple(
                sorted((timedelta(days=day) for day in self.reminder_days), reverse=True)
            ),
            renewal_wait=timedelta(minutes=self.stars_renewal_wait_minutes),
        )

    @property
    def claim_lease(self) -> timedelta:
        return timedelta(seconds=self.claim_lease_seconds)


@lru_cache
def get_settings() -> Settings:
    return Settings()
