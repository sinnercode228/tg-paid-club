"""The ``/admin`` dashboard: who gets in, what the page and the exports contain."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from paidclub.clock import FakeClock
from paidclub.config import PlanConfig
from paidclub.container import Core
from paidclub.web.admin import (
    CSS,
    Access,
    ChartPoint,
    bar_chart,
    check_access,
    create_admin_router,
    line_chart,
    nice_ticks,
)
from tests.test_stats import add_dollar_member, build_dataset

TOKEN = "s3cret-token"
PASSWORD = "пароль-владельца"
END_OF_MARCH = datetime(2026, 3, 31, 20, 0, tzinfo=UTC)


def basic(username: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def secured(core: Core) -> Core:
    core.settings = core.settings.model_copy(
        update={"admin_token": SecretStr(TOKEN), "admin_password": SecretStr(PASSWORD)}
    )
    return core


async def client_for(core: Core) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(create_admin_router(core))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://club.test")


@pytest.fixture
async def client(secured: Core) -> AsyncIterator[httpx.AsyncClient]:
    async with await client_for(secured) as instance:
        yield instance


@pytest.fixture
async def march(secured: Core, clock: FakeClock) -> Core:
    await build_dataset(secured)
    clock.set(END_OF_MARCH)
    return secured


# --- access ---------------------------------------------------------------------------------


async def test_without_credentials_the_browser_is_asked_for_them(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/admin")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer {TOKEN}x"},
        basic("admin", "wrong"),
        basic("root", PASSWORD),
        {"Authorization": "Basic not-base64!"},
        {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
        {"Authorization": "Basic " + base64.b64encode(b"admin:\xff").decode()},
        {"Authorization": f"Token {TOKEN}"},
    ],
)
async def test_wrong_credentials_are_refused(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    for path in ("/admin", "/admin/stats.json", "/admin/export/payments.csv"):
        assert (await client.get(path, headers=headers)).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [BEARER, basic("admin", PASSWORD), basic("admin", TOKEN)],
    ids=["bearer-token", "basic-password", "basic-token"],
)
async def test_token_or_password_opens_it(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    assert (await client.get("/admin", headers=headers)).status_code == 200


async def test_dashboard_is_off_without_any_secret(core: Core) -> None:
    async with await client_for(core) as client:
        response = await client.get("/admin", headers=BEARER)
    assert response.status_code == 404
    assert "PAIDCLUB_ADMIN_TOKEN" in response.json()["detail"]


def test_empty_secrets_count_as_unset(core: Core) -> None:
    settings = core.settings.model_copy(
        update={"admin_token": SecretStr(""), "admin_password": SecretStr("")}
    )
    assert check_access(settings, "Bearer ") is Access.DISABLED
    only_token = settings.model_copy(update={"admin_token": SecretStr(TOKEN)})
    assert check_access(only_token, basic("admin", "")["Authorization"]) is Access.DENIED
    assert check_access(only_token, f"bearer {TOKEN}") is Access.GRANTED


async def test_routes_stay_out_of_the_openapi_schema(secured: Core) -> None:
    app = FastAPI()
    app.include_router(create_admin_router(secured))
    assert not any(path.startswith("/admin") for path in app.openapi()["paths"])


# --- the page -------------------------------------------------------------------------------


async def test_dashboard_shows_the_month(march: Core, client: httpx.AsyncClient) -> None:
    response = await client.get("/admin?days=31", headers=BEARER)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text.replace("\xa0", " ").replace("&nbsp;", " ")

    def tile(label: str) -> str:
        found = re.search(rf'<p class="label">{label}</p><p class="value">([^<]*)</p>', html)
        assert found is not None, label
        return found.group(1)

    assert tile("Сейчас в клубе") == "7"
    assert tile("MRR") == "5 532 ₽ · 500 ⭐"
    assert tile("Выручка за 31 день") == "5 460 ₽ · 500 ⭐"
    assert tile("Новые подписки") == "2"
    assert tile("Продления") == "3"
    assert tile("Неудачные списания") == "6"
    assert tile("Отключили автопродление") == "3"
    assert tile("Ушли") == "3"
    assert "отток 30,0 %" in html
    assert "на 31.03.2026 23:00, Europe/Moscow" in html
    # Rubles and Stars each get their own revenue chart; plus the members chart.
    assert html.count("<svg") == 3
    assert "Выручка по дням, ₽" in html
    assert "Выручка по дням, ⭐" in html
    # Both money-without-effect payments are listed for the owner to refund.
    assert "Требуют внимания" in html
    assert "Период уже оплачен или есть другая подписка" in html
    assert "Платёж ссылается на неизвестную подписку" in html
    assert 'href="?days=7"' in html
    assert "<script" not in html


async def test_dollars_get_their_own_amounts_and_chart(
    march: Core, client: httpx.AsyncClient
) -> None:
    await add_dollar_member(march)
    html = (await client.get("/admin?days=31", headers=BEARER)).text
    html = html.replace("\xa0", " ").replace("&nbsp;", " ")

    assert '<p class="value">5 532 ₽ · 500 ⭐ · 10 $</p>' in html  # MRR
    assert '<p class="value">5 460 ₽ · 500 ⭐ · 10 $</p>' in html  # revenue
    assert "Выручка по дням, $" in html
    assert html.count("<svg") == 4


async def test_a_plan_priced_in_dollars_alone_shows_zero_dollars(secured: Core) -> None:
    vip = PlanConfig(code="vip", title="VIP", price_usd=Decimal("49.90"))
    secured.settings = secured.settings.model_copy(update={"plans": [vip]})
    async with await client_for(secured) as client:
        html = (await client.get("/admin", headers=BEARER)).text.replace("\xa0", " ")
    assert "<td>VIP</td><td>0</td><td>0</td><td>0 $</td>" in html


async def test_page_is_locked_down(march: Core, client: httpx.AsyncClient) -> None:
    response = await client.get("/admin", headers=BEARER)
    policy = response.headers["content-security-policy"]
    digest = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
    assert f"style-src 'sha256-{digest}'" in policy
    assert "default-src 'none'" in policy
    assert f"<style>{CSS}</style>" in response.text  # the hash matches the page's stylesheet
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_text_from_settings_is_escaped(march: Core) -> None:
    plans = [
        plan.model_copy(update={"title": "Месяц <b>VIP</b>"}) if plan.code == "month" else plan
        for plan in march.settings.plans
    ]
    march.settings = march.settings.model_copy(update={"plans": plans})
    async with await client_for(march) as client:
        html = (await client.get("/admin", headers=BEARER)).text
    assert "Месяц &lt;b&gt;VIP&lt;/b&gt;" in html
    assert "<b>VIP" not in html


@pytest.mark.parametrize("days", ["0", "367", "week"])
async def test_bad_range_is_rejected(client: httpx.AsyncClient, days: str) -> None:
    assert (await client.get(f"/admin?days={days}", headers=BEARER)).status_code == 422


async def test_long_range_charts_revenue_by_month(march: Core, client: httpx.AsyncClient) -> None:
    html = (await client.get("/admin?days=365", headers=BEARER)).text
    assert "Выручка по месяцам, ₽" in html
    assert "мар 2026" in html


async def test_empty_club_renders(secured: Core, client: httpx.AsyncClient) -> None:
    response = await client.get("/admin", headers=BEARER)
    assert response.status_code == 200
    assert "Требуют внимания" not in response.text


# --- JSON and CSV ---------------------------------------------------------------------------


async def test_stats_json(march: Core, client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/stats.json?days=31", headers=BEARER)
    data = response.json()
    assert data["now"]["live"] == 7
    assert data["totals"]["revenue"] == {"RUB": "5460.00", "XTR": "500"}
    assert data["totals"]["failed_charges"] == 6
    assert data["churn_rate"] == "0.3000"
    assert [plan["code"] for plan in data["plans"]] == ["month", "quarter", "year", "legacy15"]


@pytest.mark.parametrize(("name", "rows"), [("subscriptions", 11), ("payments", 16)])
async def test_csv_downloads(march: Core, client: httpx.AsyncClient, name: str, rows: int) -> None:
    response = await client.get(f"/admin/export/{name}.csv", headers=BEARER)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{name}-20260331-2300.csv"'
    )
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert len(response.content.decode("utf-8-sig").strip().splitlines()) == rows + 1


# --- charts ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("maximum", "ticks"),
    [
        ("0", [0, 1]),
        ("1", [0, 1]),
        ("7", [0, 2, 4, 6, 8]),
        ("264", [0, 100, 200, 300]),
        ("43000", [0, 10_000, 20_000, 30_000, 40_000, 50_000]),
        ("250", [0, 50, 100, 150, 200, 250]),
    ],
)
def test_nice_ticks_cover_the_maximum(maximum: str, ticks: list[int]) -> None:
    assert nice_ticks(Decimal(maximum)) == [Decimal(tick) for tick in ticks]


def test_charts_scale_to_the_top_tick_and_label_hover_targets() -> None:
    points = [
        ChartPoint("01.03", Decimal(0), "01.03.2026: 0 ₽"),
        ChartPoint("02.03", Decimal(50), "02.03.2026: 50 ₽"),
        ChartPoint("03.03", Decimal(100), "03.03.2026: 100 ₽ <x>"),
    ]
    bars = bar_chart("Выручка", points)
    # One rounded column per non-zero value, one hover target per point.
    assert bars.count('class="bar"') == 2
    assert bars.count('class="hit"') == 3
    assert "<title>03.03.2026: 100 ₽ &lt;x&gt;</title>" in bars
    # The tallest bar reaches the top gridline (y = 14): 100 is the last tick.
    assert re.search(r'class="bar" d="M[\d.]+,194\.0 V18\.0 A4\.0,4\.0 0 0 1 [\d.]+,14\.0', bars)

    line = line_chart("Участники", points)
    assert line.count("<polyline") == 1
    assert '<text class="end"' in line and ">100</text>" in line
