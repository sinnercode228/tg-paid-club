"""The owner's dashboard at ``/admin``: server-rendered HTML, two inline SVG charts, CSV export.

Access needs one of two settings:

* ``PAIDCLUB_ADMIN_TOKEN``: send ``Authorization: Bearer <token>`` (scripts, curl). In a browser
  the token also works as the password of the HTTP basic prompt.
* ``PAIDCLUB_ADMIN_PASSWORD`` (with ``PAIDCLUB_ADMIN_USERNAME``, ``admin`` by default): HTTP
  basic auth.

With neither set the dashboard does not exist (404). Secrets are compared in constant time.
The page has no JavaScript at all: the charts are inline SVG with native hover tooltips, and the
Content-Security-Policy allows nothing but the page's own stylesheet, pinned by its hash. Use
HTTPS in front of it, since basic auth sends the password with every request.

Mount it in the web app with ``app.include_router(create_admin_router(core))``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, tzinfo
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import SecretStr

from paidclub.config import PlanConfig, Settings
from paidclub.container import Core
from paidclub.models import Payment, PaymentOutcome
from paidclub.money import CURRENCY_SIGNS, Money
from paidclub.stats import (
    MAX_WINDOW_DAYS,
    Bucket,
    Counts,
    Report,
    club_months,
    club_report,
    export_payments,
    export_subscriptions,
    payments_needing_attention,
    report_to_dict,
    whole_units,
)
from paidclub.texts import fmt_date, plural

RANGES = (7, 30, 90, 365)
#: Above this many days the revenue chart shows months instead of days.
DAILY_BARS_LIMIT = 92
_MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


# --- auth ----------------------------------------------------------------------------------


class Access(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    #: Neither a token nor a password is configured.
    DISABLED = "disabled"


def _secret(value: SecretStr | None) -> str | None:
    """An empty secret in ``.env`` means "not set", never "the empty password"."""
    if value is None:
        return None
    return value.get_secret_value() or None


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def check_access(settings: Settings, authorization: str | None) -> Access:
    """Decide on an ``Authorization`` header. See the module docstring for the accepted forms."""
    token = _secret(settings.admin_token)
    password = _secret(settings.admin_password)
    if token is None and password is None:
        return Access.DISABLED
    if not authorization:
        return Access.DENIED
    scheme, _, credentials = authorization.partition(" ")
    scheme, credentials = scheme.lower(), credentials.strip()
    if scheme == "bearer":
        return Access.GRANTED if token is not None and _same(credentials, token) else Access.DENIED
    if scheme != "basic":
        return Access.DENIED
    try:
        decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return Access.DENIED
    username, separator, secret = decoded.partition(":")
    if not separator:
        return Access.DENIED
    user_ok = _same(username, settings.admin_username)
    # Check every configured secret, so the time taken does not tell which one matched.
    matches = [_same(secret, expected) for expected in (password, token) if expected is not None]
    return Access.GRANTED if user_ok and any(matches) else Access.DENIED


def _guard(settings: Settings) -> Callable[[Request], Awaitable[None]]:
    async def require_admin(request: Request) -> None:
        access = check_access(settings, request.headers.get("authorization"))
        if access is Access.DISABLED:
            raise HTTPException(
                404,
                detail="the admin dashboard is off: set PAIDCLUB_ADMIN_TOKEN or "
                "PAIDCLUB_ADMIN_PASSWORD",
            )
        if access is Access.DENIED:
            raise HTTPException(
                401,
                detail="authentication required",
                headers={"WWW-Authenticate": 'Basic realm="paidclub admin", charset="UTF-8"'},
            )

    return require_admin


# --- formatting ----------------------------------------------------------------------------


def _int(value: int) -> str:
    """``1 234``, grouped with a no-break space like :meth:`Money.human`."""
    return f"{value:,}".replace(",", "\u00a0")


def _amounts(values: dict[str, Money], fallback: str = "RUB") -> str:
    shown = [money.human() for money in values.values()]
    return " · ".join(shown) if shown else Money(0, fallback).human()


def _rounded(values: dict[str, Money], fallback: str = "RUB") -> str:
    return _amounts(whole_units(values), fallback)


def _percent(rate: Decimal) -> str:
    return f"{(rate * 100).quantize(Decimal('0.1')):f}".replace(".", ",") + " %"


def _compact(value: Decimal) -> str:
    """Axis labels: 0, 900, 25 тыс., 1,2 млн."""
    for size, suffix in ((Decimal(1_000_000), " млн"), (Decimal(1_000), " тыс.")):
        if abs(value) >= size:
            scaled = (value / size).quantize(Decimal("0.1")).normalize()
            return f"{scaled:f}".replace(".", ",") + suffix
    return f"{value.normalize():f}".replace(".", ",")


def _day_label(day: date) -> str:
    return f"{day:%d.%m}"


def _full_date(day: date) -> str:
    return f"{day:%d.%m.%Y}"


def _month_label(month: date) -> str:
    return f"{_MONTHS[month.month - 1]} {month.year}"


def _plan_currency(plans: Sequence[PlanConfig], code: str) -> str:
    plan = next((plan for plan in plans if plan.code == code), None)
    return plan.currencies[0] if plan is not None else "RUB"


# --- charts --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChartPoint:
    label: str
    value: Decimal
    tooltip: str


_W, _H = 720, 220
_LEFT, _RIGHT, _TOP, _BOTTOM = 56, 16, 14, 26


_STEPS = (Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5))


def nice_ticks(maximum: Decimal, most: int = 5) -> list[Decimal]:
    """Round, evenly spaced ticks from 0 up to the first one at or above ``maximum``.

    The step is the smallest of 1, 2, 2.5 or 5 times a power of ten that needs at most
    ``most`` intervals, and never below 1: every plotted value here (rubles, stars, dollars,
    members) reads naturally in whole units.
    """
    if maximum <= 0:
        return [Decimal(0), Decimal(1)]
    magnitude = Decimal(1)
    while True:
        for multiple in _STEPS:
            step = magnitude * multiple
            intervals = int((maximum / step).to_integral_value(rounding=ROUND_CEILING))
            if intervals <= most:
                return [step * i for i in range(intervals + 1)]
        magnitude *= 10


def _label_indices(n: int, wanted: int = 6) -> list[int]:
    if n <= wanted:
        return list(range(n))
    step = max(1, round((n - 1) / (wanted - 1)))
    indices = list(range(0, n - 1, step))
    if n - 1 - indices[-1] < step / 2:
        indices.pop()
    return [*indices, n - 1]


def _frame(ticks: list[Decimal], top: Decimal) -> tuple[list[str], Callable[[Decimal], float]]:
    plot_h = _H - _TOP - _BOTTOM

    def y_of(value: Decimal) -> float:
        return _TOP + plot_h - float(value / top) * plot_h if top else _TOP + plot_h

    parts = []
    for tick in ticks:
        y = y_of(tick)
        css = "base" if tick == 0 else "grid"
        parts.append(
            f'<line class="{css}" x1="{_LEFT}" x2="{_W - _RIGHT}" y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        label = escape(_compact(tick))
        parts.append(f'<text x="{_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
    return parts, y_of


def _x_labels(points: Sequence[ChartPoint], centers: list[float]) -> list[str]:
    parts = []
    indices = _label_indices(len(points))
    for i in indices:
        anchor = "middle"
        if len(points) > 1 and i == 0:
            anchor = "start"
        elif len(points) > 1 and i == len(points) - 1:
            anchor = "end"
        x = centers[i]
        if anchor == "start":
            x = max(x - 4, _LEFT)
        elif anchor == "end":
            x = min(x + 4, _W - _RIGHT)
        label = escape(points[i].label)
        parts.append(f'<text x="{x:.1f}" y="{_H - 8}" text-anchor="{anchor}">{label}</text>')
    return parts


def _svg(title: str, body: list[str]) -> str:
    return (
        f'<svg viewBox="0 0 {_W} {_H}" role="img" aria-label="{escape(title)}">'
        f"<title>{escape(title)}</title>{''.join(body)}</svg>"
    )


def bar_chart(title: str, points: Sequence[ChartPoint]) -> str:
    """Columns from a zero baseline, at most 24 px wide, rounded at the data end only."""
    if not points:
        return _svg(title, [])
    ticks = nice_ticks(max(point.value for point in points))
    top = ticks[-1]
    parts, y_of = _frame(ticks, top)
    base = y_of(Decimal(0))
    slot = (_W - _LEFT - _RIGHT) / len(points)
    width = max(1.0, min(24.0, slot * 0.7, slot - 2))
    centers = [_LEFT + slot * (i + 0.5) for i in range(len(points))]
    for point, center in zip(points, centers, strict=True):
        x = center - width / 2
        y = y_of(point.value)
        mark = ""
        height = base - y
        if height > 0:
            r = min(4.0, width / 2, height)
            mark = (
                f'<path class="bar" d="M{x:.1f},{base:.1f} V{y + r:.1f} '
                f"A{r:.1f},{r:.1f} 0 0 1 {x + r:.1f},{y:.1f} H{x + width - r:.1f} "
                f'A{r:.1f},{r:.1f} 0 0 1 {x + width:.1f},{y + r:.1f} V{base:.1f} Z"/>'
            )
        parts.append(
            f'<g class="slot"><title>{escape(point.tooltip)}</title>'
            f'<rect class="hit" x="{center - slot / 2:.1f}" y="{_TOP}" width="{slot:.1f}" '
            f'height="{base - _TOP:.1f}"/>{mark}</g>'
        )
    parts += _x_labels(points, centers)
    return _svg(title, parts)


def line_chart(title: str, points: Sequence[ChartPoint]) -> str:
    """A 2 px line with a light wash under it and the last value labelled at its end."""
    if not points:
        return _svg(title, [])
    ticks = nice_ticks(max(point.value for point in points))
    parts, y_of = _frame(ticks, ticks[-1])
    base = y_of(Decimal(0))
    plot_w = _W - _LEFT - _RIGHT - 36  # room for the end label
    step = plot_w / max(len(points) - 1, 1)
    xs = [_LEFT + step * i for i in range(len(points))]
    if len(points) == 1:
        xs = [_LEFT + plot_w / 2]
    ys = [y_of(point.value) for point in points]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    parts.append(
        f'<polygon class="area" points="{xs[0]:.1f},{base:.1f} {line} {xs[-1]:.1f},{base:.1f}"/>'
    )
    parts.append(f'<polyline class="line" points="{line}"/>')
    slot = step if len(points) > 1 else plot_w
    for point, x in zip(points, xs, strict=True):
        parts.append(
            f'<g class="slot"><title>{escape(point.tooltip)}</title>'
            f'<rect class="hit" x="{x - slot / 2:.1f}" y="{_TOP}" width="{slot:.1f}" '
            f'height="{base - _TOP:.1f}"/></g>'
        )
    parts.append(f'<circle class="dot" cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4"/>')
    parts.append(
        f'<text class="end" x="{xs[-1] + 8:.1f}" y="{ys[-1] + 4:.1f}">'
        f"{escape(_int(int(points[-1].value)))}</text>"
    )
    parts += _x_labels(points, xs)
    return _svg(title, parts)


# --- page ----------------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series:#2a78d6;
--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink-2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series:#3987e5;--critical:#e66767}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-end;
justify-content:space-between;margin-bottom:20px}
h1{font-size:22px;margin:0}
h2{font-size:15px;font-weight:600;margin:0 0 12px}
p{margin:0}
.muted{color:var(--muted)}
.ranges{display:flex;gap:6px;flex-wrap:wrap}
.ranges a{color:var(--ink-2);text-decoration:none;border:1px solid var(--border);
border-radius:999px;padding:4px 12px;font-size:14px}
.ranges a[aria-current]{color:var(--ink);font-weight:600;border-color:var(--ink-2)}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
@media (max-width:760px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tile,.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
padding:14px 16px}
.tile .label{color:var(--ink-2);font-size:13px}
.tile .value{font-size:26px;font-weight:600;margin:2px 0 4px}
.tile .note{color:var(--muted);font-size:13px}
.card{margin-bottom:16px}
.card.alert{border-color:var(--critical)}
.hint{color:var(--muted);font-size:13px;margin:-6px 0 10px}
svg{display:block;width:100%;height:auto}
svg text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
svg .end{fill:var(--ink-2);font-size:12px;font-weight:600}
.grid{stroke:var(--grid);stroke-width:1}
.base{stroke:var(--axis);stroke-width:1}
.bar{fill:var(--series)}
.line{fill:none;stroke:var(--series);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.area{fill:var(--series);fill-opacity:.1}
.dot{fill:var(--series);stroke:var(--surface);stroke-width:2}
.hit{fill:transparent}
.slot:hover .hit{fill:var(--grid);fill-opacity:.6}
.table{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--grid);white-space:nowrap;
font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink-2);font-weight:600}
details summary{cursor:pointer;font-weight:600}
details[open] summary{margin-bottom:12px}
footer{color:var(--ink-2);font-size:14px;display:flex;gap:16px;flex-wrap:wrap}
footer a{color:var(--ink)}
""".strip()

_CSS_HASH = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
PAGE_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'sha256-{_CSS_HASH}'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex",
}

_ATTENTION = {
    PaymentOutcome.NEEDS_REFUND: "Период уже оплачен или есть другая подписка — верните деньги",
    PaymentOutcome.AMOUNT_MISMATCH: "Сумма не совпадает с ценой подписки",
    PaymentOutcome.UNKNOWN_SUBSCRIPTION: "Платёж ссылается на неизвестную подписку",
}


_CURRENT = ' aria-current="page"'


def _tile(label: str, value: str, note: str = "") -> str:
    # "333 370 ₽ · 3 000 ⭐" may wrap between the amounts, never inside one.
    value_html = " · ".join(escape(part).replace(" ", "&nbsp;") for part in value.split(" · "))
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    return (
        f'<div class="tile"><p class="label">{escape(label)}</p>'
        f'<p class="value">{value_html}</p>{note_html}</div>'
    )


def _table(head: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    th = "".join(f"<th>{escape(cell)}</th>" for cell in head)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<div class="table"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def _by_month(days: Sequence[Bucket]) -> list[Bucket]:
    """Fold daily buckets into calendar months (live = the last day's)."""
    counts: dict[date, Counts] = {}
    live: dict[date, int] = {}
    for day in days:
        month = day.start.replace(day=1)
        counts.setdefault(month, Counts()).merge(day.counts)
        live[month] = day.live
    return [Bucket(start=month, counts=counts[month], live=live[month]) for month in counts]


def _since_first_activity(months: Sequence[Bucket]) -> list[Bucket]:
    """Drop the empty months before the club had any members or payments."""
    for index, month in enumerate(months):
        if month.live or month.counts != Counts():
            return list(months[index:])
    return []


def _revenue_chart(report: Report, currency: str) -> str:
    sign = CURRENCY_SIGNS[currency]
    daily = len(report.days) <= DAILY_BARS_LIMIT
    buckets = report.days if daily else _by_month(report.days)
    points = []
    for bucket in buckets:
        money = bucket.counts.revenue_in(currency)
        label = _day_label(bucket.start) if daily else _month_label(bucket.start)
        when = _full_date(bucket.start) if daily else _month_label(bucket.start)
        payments = bucket.counts.payments
        points.append(
            ChartPoint(
                label=label,
                value=money.to_decimal(),
                tooltip=f"{when}: {money.human()}, "
                f"{payments} {plural(payments, 'платёж', 'платежа', 'платежей')}",
            )
        )
    title = f"Выручка по {'дням' if daily else 'месяцам'}, {sign}"
    return f'<section class="card"><h2>{escape(title)}</h2>{bar_chart(title, points)}</section>'


def _live_chart(report: Report) -> str:
    long_window = len(report.days) > DAILY_BARS_LIMIT
    points = [
        ChartPoint(
            label=f"{day.start:%d.%m.%y}" if long_window else _day_label(day.start),
            value=Decimal(day.live),
            tooltip=f"{_full_date(day.start)}: {day.live} в клубе, новых {day.counts.new}, "
            f"ушли {day.counts.churned}",
        )
        for day in report.days
    ]
    title = "Участников в клубе на конец дня"
    return f'<section class="card"><h2>{escape(title)}</h2>{line_chart(title, points)}</section>'


def render_dashboard(
    report: Report,
    months: Sequence[Bucket],
    attention: Sequence[Payment],
    *,
    days: int,
    plans: Sequence[PlanConfig],
) -> str:
    tz: tzinfo = report.tz
    now, totals = report.snapshot.total, report.totals
    generated = report.snapshot.at
    nav = "".join(
        f'<a href="?days={n}"{_CURRENT if n == days else ""}>'
        f"{n} {plural(n, 'день', 'дня', 'дней')}</a>"
        for n in RANGES
    )
    churn = report.churn_rate
    tiles = [
        _tile(
            "Сейчас в клубе",
            _int(now.live),
            f"автопродление {now.active} · льготный период {now.past_due} · "
            f"отключили {now.canceled}",
        ),
        _tile("MRR", _rounded(now.mrr), "по подпискам с автопродлением"),
        _tile(
            f"Выручка за {days} {plural(days, 'день', 'дня', 'дней')}",
            _amounts(totals.revenue),
            f"{totals.payments} {plural(totals.payments, 'платёж', 'платежа', 'платежей')}",
        ),
        _tile(
            "Новые подписки",
            _int(totals.new),
            f"вернулись после окончания: {totals.reactivated}" if totals.reactivated else "",
        ),
        _tile("Продления", _int(totals.renewals)),
        _tile(
            "Неудачные списания",
            _int(totals.failed_charges),
            f"продление не прошло: {totals.dunning_started}, "
            f"оплатили в льготный период: {totals.recovered}"
            + (
                f" · без автосписания: {totals.renewals_not_charged}"
                if totals.renewals_not_charged
                else ""
            ),
        ),
        _tile(
            "Отключили автопродление",
            _int(totals.cancellations),
            f"включили обратно: {totals.resumed}",
        ),
        _tile(
            "Ушли",
            _int(totals.churned),
            (f"отток {_percent(churn)} · " if churn is not None else "")
            + f"не оплатили {totals.churned_unpaid}, после отключения "
            f"{totals.churned_after_cancel}",
        ),
    ]

    sections = [f'<section class="tiles">{"".join(tiles)}</section>']
    for currency in report.currencies or ["RUB"]:
        if currency == "RUB" or totals.revenue_minor.get(currency):
            sections.append(_revenue_chart(report, currency))
    sections.append(_live_chart(report))

    plan_rows = [
        [
            plan.title,
            _int(plan.now.live),
            _int(plan.now.active),
            _rounded(plan.now.mrr, _plan_currency(plans, plan.code)),
            _int(plan.period.new),
            _int(plan.period.renewals),
            _int(plan.period.churned),
            _amounts(plan.period.revenue, _plan_currency(plans, plan.code)),
        ]
        for plan in report.plans
    ]
    sections.append(
        '<section class="card"><h2>По тарифам</h2>'
        f'<p class="hint">Сейчас и за {days} {plural(days, "день", "дня", "дней")}</p>'
        + _table(
            [
                "Тариф",
                "В клубе",
                "Автопродление",
                "MRR",
                "Новые",
                "Продления",
                "Ушли",
                "Выручка",
            ],
            plan_rows,
        )
        + "</section>"
    )

    month_rows = [
        [
            _month_label(month.start),
            _int(month.counts.new),
            _int(month.counts.renewals),
            _int(month.counts.failed_charges),
            _int(month.counts.cancellations),
            _int(month.counts.churned),
            _amounts(month.counts.revenue),
            _int(month.live),
        ]
        for month in reversed(_since_first_activity(months))
    ]
    sections.append(
        '<section class="card"><h2>По месяцам</h2>'
        + _table(
            [
                "Месяц",
                "Новые",
                "Продления",
                "Неудачные списания",
                "Отключили",
                "Ушли",
                "Выручка",
                "В клубе на конец",
            ],
            month_rows,
        )
        + "</section>"
    )

    if attention:
        attention_rows = [
            [
                f"{payment.created_at.astimezone(tz):%d.%m.%Y %H:%M}",
                f"{payment.provider} {payment.provider_payment_id}",
                f"#{payment.subscription_id}" if payment.subscription_id else "—",
                Money(payment.amount_minor, payment.currency).human(),
                _ATTENTION.get(payment.outcome, payment.outcome.value),
            ]
            for payment in attention
        ]
        sections.append(
            '<section class="card alert"><h2>Требуют внимания: деньги пришли, но не зачислены</h2>'
            + _table(["Когда", "Платёж", "Подписка", "Сумма", "Что случилось"], attention_rows)
            + "</section>"
        )

    day_rows = [
        [
            _full_date(day.start),
            _int(day.counts.new),
            _int(day.counts.renewals),
            _int(day.counts.failed_charges),
            _int(day.counts.churned),
            _amounts(day.counts.revenue),
            _int(day.live),
        ]
        for day in reversed(report.days)
    ]
    sections.append(
        '<section class="card"><details><summary>Таблица по дням</summary>'
        + _table(
            ["День", "Новые", "Продления", "Неудачные списания", "Ушли", "Выручка", "В клубе"],
            day_rows,
        )
        + "</details></section>"
    )

    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex"><title>Статистика клуба</title>'
        f'<style>{CSS}</style></head><body><div class="wrap">'
        "<header><div><h1>Статистика клуба</h1>"
        f'<p class="muted">на {fmt_date(generated, tz)} {generated.astimezone(tz):%H:%M}, '
        f"{escape(str(tz))}</p>"
        f'</div><nav class="ranges" aria-label="Период">{nav}</nav></header>'
        f"<main>{''.join(sections)}</main>"
        '<footer><span>Выгрузка CSV:</span><a href="admin/export/subscriptions.csv">подписки</a>'
        '<a href="admin/export/payments.csv">платежи</a>'
        f'<a href="admin/stats.json?days={days}">JSON</a></footer>'
        "</div></body></html>"
    )


# --- routes --------------------------------------------------------------------------------


def _csv_response(content: bytes, name: str, core: Core) -> Response:
    stamp = core.clock.now().astimezone(core.tz).strftime("%Y%m%d-%H%M")
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}-{stamp}.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


Days = Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)]


def create_admin_router(core: Core) -> APIRouter:
    router = APIRouter(
        prefix="/admin", include_in_schema=False, dependencies=[Depends(_guard(core.settings))]
    )

    @router.get("", response_class=HTMLResponse)
    async def dashboard(days: Days = 30) -> HTMLResponse:
        report = await club_report(core, days=days)
        months = await club_months(core, months=12)
        async with core.sessions() as session:
            attention = await payments_needing_attention(session)
        html = render_dashboard(report, months, attention, days=days, plans=core.settings.plans)
        return HTMLResponse(html, headers=PAGE_HEADERS)

    @router.get("/stats.json")
    async def stats_json(days: Days = 30) -> JSONResponse:
        report = await club_report(core, days=days)
        return JSONResponse(report_to_dict(report), headers={"Cache-Control": "no-store"})

    @router.get("/export/subscriptions.csv")
    async def subscriptions_csv() -> Response:
        async with core.sessions() as session:
            content = await export_subscriptions(session, core.tz)
        return _csv_response(content, "subscriptions", core)

    @router.get("/export/payments.csv")
    async def payments_csv() -> Response:
        async with core.sessions() as session:
            content = await export_payments(session, core.tz)
        return _csv_response(content, "payments", core)

    return router
