"""Settings, loaded from environment variables (prefix ``PAIDCLUB_``) or ``.env``.

List settings accept either JSON (``[1, 3]``) or a comma-separated string (``1,3``), so a
``.env`` line like ``PAIDCLUB_CLUB_CHAT_IDS=-1001234567890`` works as expected. Plans are JSON.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from paidclub.domain.subscriptions import BillingPolicy, PeriodUnit
from paidclub.money import CURRENCY_EXPONENTS, Money

ProviderName = Literal["fake", "yookassa", "stars"]

#: Telegram Stars subscriptions renew every 30 days; it is the only period the Bot API accepts.
STARS_SUBSCRIPTION_PERIOD = timedelta(days=30)

#: Where Telegram delivers updates in webhook mode, under ``public_base_url``.
TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"
#: The page that sends a member back to the bot after a provider's payment page.
RETURN_PAGE_PATH = "/paid"
#: The Bot API's rule for ``setWebhook``'s ``secret_token``.
TELEGRAM_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}")
#: A secret path segment of a webhook URL: URL-safe and long enough not to be guessed.
PATH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
#: ``<bot id>:<secret>`` as @BotFather issues it.
BOT_TOKEN_PATTERN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
#: Accepted URL schemes and the async driver each one runs on.
ASYNC_DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "sqlite+aiosqlite": "sqlite+aiosqlite",
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "postgresql+asyncpg": "postgresql+asyncpg",
}


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
    #: Price in US dollars for crypto invoices, e.g. ``"10.00"``; the member pays it in a coin.
    price_usd: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_prices(self) -> Self:
        if self.price_rub is None and self.price_stars is None and self.price_usd is None:
            raise ValueError(
                f"plan {self.code!r} needs price_rub, price_stars, price_usd or several of them"
            )
        if self.price_rub is not None:
            Money.from_decimal(self.price_rub, "RUB")  # rejects more than two decimal places
        if self.price_usd is not None:
            Money.from_decimal(self.price_usd, "USD")  # cents at most
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
        if currency == "USD" and self.price_usd is not None:
            return Money.from_decimal(self.price_usd, "USD")
        return None

    @property
    def currencies(self) -> list[str]:
        """The currencies the plan has a price in, rubles first, then Stars, then dollars."""
        return [code for code in CURRENCY_EXPONENTS if self.price_for(code) is not None]

    @property
    def months_equivalent(self) -> Decimal:
        """Length in months, used to normalise prices to MRR (30 days count as one month)."""
        if self.period_unit == "month":
            return Decimal(self.period_count)
        return Decimal(self.period_count) / Decimal(30)


DEFAULT_PLANS: tuple[PlanConfig, ...] = (
    PlanConfig(
        code="month",
        title="1 месяц",
        period_count=1,
        price_rub=Decimal("990.00"),
        price_stars=500,
        price_usd=Decimal("10.00"),
    ),
    PlanConfig(
        code="quarter",
        title="3 месяца",
        period_count=3,
        price_rub=Decimal("2490.00"),
        price_usd=Decimal("25.00"),
    ),
    PlanConfig(
        code="year",
        title="12 месяцев",
        period_count=12,
        price_rub=Decimal("8900.00"),
        price_usd=Decimal("90.00"),
    ),
)


def _only_fake() -> list[ProviderName]:
    return ["fake"]


def _http_url(value: str) -> str:
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"must be an http:// or https:// URL, got {value!r}")
    return url


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
        # ``PAIDCLUB_OWNER_ID=`` in a copied .env.example means "not set", not "invalid".
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- runtime ---------------------------------------------------------------------------
    #: ``prod`` refuses the fake provider and hides the API docs. The Docker image sets it.
    env: Literal["dev", "test", "prod"] = "dev"
    #: This deployment is a public demo: allow the fake provider, whose payment page lets
    #: anyone "pay" without money, for real club chats on a public address.
    demo: bool = False
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
    #: ``polling`` asks Telegram for updates (no public URL needed); ``webhook`` has Telegram
    #: push them to ``{public_base_url}/telegram/webhook``, which must be HTTPS.
    bot_mode: Literal["polling", "webhook"] = "polling"
    #: Telegram sends it back in ``X-Telegram-Bot-Api-Secret-Token`` with every webhook call.
    telegram_webhook_secret: SecretStr | None = None

    # --- web -------------------------------------------------------------------------------
    #: Address and port the web app (webhooks, checkout page, dashboard) listens on. The
    #: Docker image sets the host to ``0.0.0.0``.
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8000, ge=0, le=65535)
    #: Public base URL of the FastAPI app (checkout pages, webhooks, return URLs).
    public_base_url: str = "http://localhost:8000"
    #: Bearer token for ``/admin``. Either this or ``admin_password`` must be set to open it.
    admin_token: SecretStr | None = None
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    #: Where providers send the member after paying, e.g. ``https://t.me/<your_bot>``. The
    #: default is the app's own page, see :attr:`after_payment_url`.
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
    #: A secret path segment for YooKassa notifications. Set, they are accepted only at
    #: ``{public_base_url}/webhooks/yookassa/<token>`` (the URL put in the shop settings), so a
    #: forged notification sent anywhere else costs no request to the shop API.
    yookassa_webhook_token: SecretStr | None = None

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

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        url = value.strip()
        scheme, sep, rest = url.partition("://")
        if not sep:
            raise ValueError(
                "must be a URL like sqlite+aiosqlite:///./paidclub.db or "
                "postgresql+asyncpg://user:password@host:5432/paidclub"
            )
        # The app is async: plain postgresql:// and sqlite:// would load sync drivers.
        driver = ASYNC_DRIVERS.get(scheme.lower())
        if driver is None:
            raise ValueError(
                f"unsupported database {scheme!r}: use sqlite+aiosqlite or postgresql+asyncpg"
            )
        return f"{driver}://{rest}"

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}, got {value!r}")
        return level

    @field_validator("display_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown timezone {value!r}, use an IANA name like Europe/Moscow"
            ) from exc
        return value

    @field_validator("bot_token")
    @classmethod
    def _check_bot_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if not BOT_TOKEN_PATTERN.fullmatch(value.get_secret_value()):
            # The token itself is not echoed: it may be a real one with a typo.
            raise ValueError("does not look like a bot token from @BotFather (123456:ABC-...)")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        # Paths are appended to it (``/webhooks/yookassa``), so no trailing slash or query.
        url = _http_url(value).rstrip("/")
        if urlsplit(url).query or urlsplit(url).fragment:
            raise ValueError(f"must not have a query string or fragment, got {value!r}")
        return url

    @field_validator("checkout_return_url")
    @classmethod
    def _check_return_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _http_url(value)

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _check_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        if not TELEGRAM_SECRET_PATTERN.fullmatch(value.get_secret_value()):
            raise ValueError("Telegram accepts 1-256 characters: letters A-Z, a-z, digits, _ and -")
        return value

    @field_validator("yookassa_webhook_token")
    @classmethod
    def _check_path_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        if not PATH_TOKEN_PATTERN.fullmatch(value.get_secret_value()):
            raise ValueError("must be 16-128 characters: letters A-Z, a-z, digits, _ and -")
        return value

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
        if self.bot_mode == "webhook":
            if not self.public_base_url.startswith("https://"):
                raise ValueError(
                    "bot_mode=webhook needs an https:// public_base_url: Telegram only "
                    "delivers webhooks over HTTPS"
                )
            if self.telegram_webhook_secret is None:
                raise ValueError("bot_mode=webhook needs telegram_webhook_secret")
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

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_base_url}{TELEGRAM_WEBHOOK_PATH}"

    @property
    def after_payment_url(self) -> str:
        """Where the member lands after a provider's payment page: ``checkout_return_url``, or
        the app's page that sends them back to the bot."""
        return self.checkout_return_url or f"{self.public_base_url}{RETURN_PAGE_PATH}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
