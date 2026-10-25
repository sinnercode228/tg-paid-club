"""Settings mistakes as messages that name the variable to fix.

* :func:`load_settings` reads the settings and raises :class:`ConfigError` with one line per
  problem, named by its environment variable (``PAIDCLUB_OWNER_ID: Input should be a valid
  integer``), instead of a pydantic traceback. Values are never echoed: some are secrets.
* :func:`startup_problems` lists what a valid configuration still lacks for a command:
  ``run`` cannot start without a bot token (an error), and a dashboard without a password is
  only switched off (a warning). In prod a short dashboard secret is refused. The fake
  provider is refused where its free payment page would be reachable by strangers, unless
  ``PAIDCLUB_DEMO=1`` says that is the point.
* :func:`describe` is the summary ``paidclub check`` prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy.engine import make_url

from paidclub.config import Settings

ENV_PREFIX = "PAIDCLUB_"

#: Ports ``setWebhook`` accepts in the webhook URL.
TELEGRAM_WEBHOOK_PORTS = (443, 80, 88, 8443)
#: Ports YooKassa delivers notifications to (HTTPS only).
YOOKASSA_WEBHOOK_PORTS = (443, 8443)
#: /admin has no limit on login attempts, so its token or password must not be guessable.
MIN_ADMIN_SECRET_LENGTH = 16

Command = Literal["run", "web"]


class ConfigError(Exception):
    """The settings could not be loaded; ``lines`` says what to fix, one problem per line."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        super().__init__("\n".join(lines))


def env_name(field: str) -> str:
    return f"{ENV_PREFIX}{field.upper()}"


def _location(loc: tuple[int | str, ...]) -> str:
    head, *rest = loc
    name = env_name(head) if isinstance(head, str) and head in Settings.model_fields else str(head)
    for part in rest:
        name += f"[{part}]" if isinstance(part, int) else f".{part}"
    return name


_SNAKE_WORD = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


def _with_env_names(message: str) -> str:
    """``needs yookassa_shop_id`` -> ``needs PAIDCLUB_YOOKASSA_SHOP_ID``; other words stay."""

    def replace(match: re.Match[str]) -> str:
        word = match.group()
        return env_name(word) if word in Settings.model_fields else word

    return _SNAKE_WORD.sub(replace, message)


def describe_validation_error(exc: ValidationError) -> list[str]:
    lines = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        message = _with_env_names(error["msg"].removeprefix("Value error, "))
        # Errors between settings (``loc == ()``) name their variables in the message.
        lines.append(f"{_location(error['loc'])}: {message}" if error["loc"] else message)
    return lines


_SOURCE_FIELD = re.compile(r'field "(\w+)"')


def load_settings(**overrides: Any) -> Settings:
    """``Settings(**overrides)``, with errors turned into a :class:`ConfigError`."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(describe_validation_error(exc)) from None
    except SettingsError as exc:
        # pydantic-settings could not decode a JSON value (plans) before validation.
        found = _SOURCE_FIELD.search(str(exc))
        name = env_name(found.group(1)) if found else "settings"
        raise ConfigError([f"{name}: not valid JSON"]) from None


@dataclass(frozen=True, slots=True)
class Problem:
    severity: Literal["error", "warning"]
    message: str


def _port(url: str) -> int:
    parts = urlsplit(url)
    return parts.port or (443 if parts.scheme == "https" else 80)


def startup_problems(settings: Settings, command: Command = "run") -> list[Problem]:
    """What ``settings`` lacks for ``command``. Errors stop the start, warnings are logged."""
    problems: list[Problem] = []

    def error(message: str) -> None:
        problems.append(Problem("error", message))

    def warning(message: str) -> None:
        problems.append(Problem("warning", message))

    if command == "run":
        if settings.bot_token is None:
            error(
                "PAIDCLUB_BOT_TOKEN is not set: create a bot with @BotFather and put its token here"
            )
        if settings.owner_id is None:
            warning(
                "PAIDCLUB_OWNER_ID is not set: nobody can use /stats and /export, and alerts "
                "(missing rights in a club chat, payments that need a refund) go nowhere"
            )
        if not settings.club_chat_ids:
            warning(
                "PAIDCLUB_CLUB_CHAT_IDS is empty: members can pay, but there is no chat to "
                "let them into"
            )
        if (
            settings.bot_mode == "webhook"
            and _port(settings.public_base_url) not in TELEGRAM_WEBHOOK_PORTS
        ):
            error(
                f"PAIDCLUB_PUBLIC_BASE_URL uses port {_port(settings.public_base_url)}: Telegram "
                f"delivers webhooks only to ports {', '.join(map(str, TELEGRAM_WEBHOOK_PORTS))}"
            )

    if "yookassa" in settings.providers and (
        not settings.public_base_url.startswith("https://")
        or _port(settings.public_base_url) not in YOOKASSA_WEBHOOK_PORTS
    ):
        warning(
            "YooKassa sends notifications only to HTTPS on port 443 or 8443, so "
            f"{settings.public_base_url}/webhooks/yookassa will not receive them, and a "
            "first payment is applied only when its notification arrives"
        )
    if (
        "yookassa" in settings.providers
        and settings.yookassa_webhook_token is None
        and not _is_local(settings.public_base_url)
    ):
        warning(
            "PAIDCLUB_YOOKASSA_WEBHOOK_TOKEN is not set: anyone can post to /webhooks/yookassa, "
            "and checking each post costs a request to the shop API. Set it and give "
            f"{settings.public_base_url}/webhooks/yookassa/<token> as the notification URL in "
            "the shop settings, or let only YooKassa's addresses through at the proxy"
        )
    if command in ("run", "web") and not settings.demo:
        for message in _fake_provider_exposed(settings):
            error(message)
    if "fake" in settings.providers and "fake_webhook_secret" not in settings.model_fields_set:
        warning(
            "PAIDCLUB_FAKE_WEBHOOK_SECRET is not set: a random one is made at every start, so "
            "demo checkout links from before a restart stop working"
        )
    if not _secret_set(settings.admin_token) and not _secret_set(settings.admin_password):
        warning(
            "the /admin dashboard is off (404): set PAIDCLUB_ADMIN_TOKEN or "
            "PAIDCLUB_ADMIN_PASSWORD to open it"
        )
    for field in ("admin_token", "admin_password"):
        value = getattr(settings, field)
        if not _secret_set(value) or len(value.get_secret_value()) >= MIN_ADMIN_SECRET_LENGTH:
            continue
        message = (
            f"{env_name(field)} is shorter than {MIN_ADMIN_SECRET_LENGTH} characters: /admin "
            "and its CSV export of every member do not limit login attempts, so a short "
            "secret can be guessed. Use a random one, e.g. from "
            "python3 -c 'import secrets; print(secrets.token_urlsafe(24))'"
        )
        if settings.env == "prod":
            error(message)
        elif not _is_local(settings.public_base_url):
            warning(message)
    return problems


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _is_local(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


def _fake_provider_exposed(settings: Settings) -> list[str]:
    """The fake provider's page lets anyone "pay" without money. That is the demo; next to a
    real provider, or for real club chats on a public address, it gives the club away."""
    if "fake" not in settings.providers:
        return []
    real = [name for name in settings.providers if name != "fake"]
    if real:
        return [
            f"PAIDCLUB_PROVIDERS has fake next to {', '.join(real)}: members would be offered "
            "the demo payment page, where anyone pays without money. Remove fake, or set "
            "PAIDCLUB_DEMO=1 if this is a demo"
        ]
    if settings.club_chat_ids and not _is_local(settings.public_base_url):
        return [
            "PAIDCLUB_PROVIDERS=fake with club chats at a public "
            f"PAIDCLUB_PUBLIC_BASE_URL ({settings.public_base_url}): anyone could join through "
            "the demo payment page without paying. Use yookassa or stars, or set "
            "PAIDCLUB_DEMO=1 if this is a demo"
        ]
    return []


def _secret_set(value: Any) -> bool:
    return value is not None and bool(value.get_secret_value())


def safe_database_url(url: str) -> str:
    """The URL with its password masked, for logs and ``paidclub check``."""
    return make_url(url).render_as_string(hide_password=True)


def describe(settings: Settings) -> list[str]:
    """What ``paidclub run`` would use, without secrets."""
    plans = []
    for plan in settings.plans:
        prices = []
        if plan.price_rub is not None:
            prices.append(f"{plan.price_rub} RUB")
        if plan.price_stars is not None:
            prices.append(f"{plan.price_stars} XTR")
        unit = "month" if plan.period_unit == "month" else "day"
        plans.append(
            f"{plan.code} ({plan.period_count} {unit}{'s' if plan.period_count > 1 else ''}, "
            f"{' / '.join(prices)})"
        )
    admin = (
        "bearer token"
        if _secret_set(settings.admin_token)
        else "basic auth"
        if _secret_set(settings.admin_password)
        else "off"
    )

    def days(values: list[float]) -> str:
        return ", ".join(f"{value:g}" for value in values) or "none"

    return [
        f"env: {settings.env}" + (", demo" if settings.demo else ""),
        f"database: {safe_database_url(settings.database_url)}",
        f"bot: {settings.bot_mode}, token {'set' if settings.bot_token else 'not set'}, "
        f"owner {settings.owner_id if settings.owner_id is not None else 'not set'}, "
        f"club chats {', '.join(map(str, settings.club_chat_ids)) or 'none'}",
        f"web: listens on {settings.web_host}:{settings.web_port}, public URL "
        f"{settings.public_base_url}, /admin {admin}",
        f"providers: {', '.join(settings.providers)}",
        *(
            [
                f"yookassa notification URL: {settings.public_base_url}/webhooks/yookassa"
                + ("/<PAIDCLUB_YOOKASSA_WEBHOOK_TOKEN>" if settings.yookassa_webhook_token else "")
            ]
            if "yookassa" in settings.providers
            else []
        ),
        f"plans: {'; '.join(plans)}",
        f"billing: grace {settings.grace_days:g} days, retries after "
        f"{days(settings.dunning_retry_days)} days, reminders "
        f"{days(settings.reminder_days)} days before",
    ]
