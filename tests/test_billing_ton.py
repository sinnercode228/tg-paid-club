"""USDT straight to the owner's TON wallet: checkout page, pinning the jetton wallet, reading it
once per pass, and the money rules. toncenter is an in-memory fake whose transactions have the
shape of real ones (tests/fixtures/toncenter_jetton_credit.json is a real one, trimmed)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from paidclub import club
from paidclub.billing import ProviderRegistry
from paidclub.billing.base import (
    CheckoutRequest,
    PaymentFailed,
    RenewalRequest,
    WebhookRejected,
)
from paidclub.billing.ton import (
    USDT_MASTER,
    TonError,
    TonProvider,
    format_units,
    is_credit,
    normalize_comment,
    read_credit,
)
from paidclub.billing.tonaddress import TonAddress, address_cell_boc, crc32c, read_boc_address
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.domain.subscriptions import SubStatus, renewal_idempotence_key
from paidclub.models import (
    PaymentOutcome,
    TonInvoice,
    TonInvoiceStatus,
    TonTransfer,
    TonWallet,
)
from paidclub.money import Money
from paidclub.scheduler import Scheduler
from paidclub.ton_payments import LATE_POLL, LATE_WATCH, TonReport, poll_ton_wallet
from paidclub.web.ton_checkout import create_ton_checkout_router
from tests.helpers import FakeRevoker, load_sub, notifications, payments

FIXTURES = Path(__file__).parent / "fixtures"


def _address(seed: str) -> TonAddress:
    return TonAddress(0, hashlib.sha256(seed.encode()).digest())


OWNER = _address("the owner's wallet")
#: What the USDT master answers for OWNER: the only wallet whose credits count.
JETTON_WALLET = _address("the owner's USDT jetton wallet")
#: A contract the attacker deployed that claims to be a USDT wallet of the owner.
FAKE_WALLET = _address("a fake jetton wallet")
PAYER = _address("a member's wallet")
PAYER_JETTON_WALLET = _address("the member's USDT jetton wallet")
MASTER = TonAddress.parse(USDT_MASTER)
MEMBER = 777
USDT = 1_000_000  # units in one USDT
PRICE = 10 * USDT  # the default monthly plan: 10.00 $
POLL = timedelta(seconds=15)


def _b64(seed: str) -> str:
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


def _with_crc(boc: str) -> str:
    """The same bag of cells with a CRC32-C, as toncenter writes them."""
    data = bytearray(base64.b64decode(boc))
    data[4] |= 0x40
    return base64.b64encode(bytes(data) + crc32c(bytes(data)).to_bytes(4, "little")).decode()


class FakeToncenter:
    """toncenter behind ``httpx.MockTransport``: ``POST /api/v2/runGetMethodStd`` running the
    USDT master's ``get_wallet_address``, and ``GET /api/v3/transactions`` filtered by account,
    ``start_lt``/``start_utime``, ``sort`` and ``limit``. Transactions are built in the shape of
    real ones. ``script`` queues raw responses or transport errors for the next requests."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.transactions: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response | Exception] = []
        #: When set, start_lt is ignored, like a misbehaving indexer.
        self.ignore_start_lt = False
        #: Transactions of other accounts returned with every answer, as a buggy (or lied to)
        #: indexer might.
        self.leaked: list[dict[str, Any]] = []
        self.lt = 60_000_000_000_000

    # --- test controls ---------------------------------------------------------------------

    def credit(
        self,
        units: int,
        comment: str | None,
        *,
        account: TonAddress = JETTON_WALLET,
        destination: TonAddress | None = None,
        opcode: Any = "0x178d4519",
        aborted: bool = False,
        compute_success: bool = True,
        bounced: bool = False,
        finality: Any = "finalized",
        decoded: bool = True,
        inline: bool = False,
    ) -> dict[str, Any]:
        """A transaction on ``account`` that received an ``internal_transfer``."""
        self.lt += 1_000
        payload: dict[str, Any] = {"@type": "first_from_either" if inline else "second_from_either"}
        payload["value"] = {"@type": "text_comment", "text": comment} if comment is not None else {}
        body = {
            "@type": "jetton_internal_transfer",
            "query_id": "0",
            "amount": {"@type": "var_uint", "len": "4", "value": str(units)},
            "from": {
                "@type": "addr_std",
                "anycast": {"@type": "nothing_from_maybe"},
                "workchain_id": "0",
                "address": PAYER.account.hex().upper(),
            },
            "forward_ton_amount": {"@type": "var_uint", "len": "1", "value": "1"},
            "forward_payload": payload,
        }
        tx: dict[str, Any] = {
            "account": account.raw,
            "hash": _b64(f"tx {self.lt}"),
            "lt": str(self.lt),
            "now": int(self.clock.now().timestamp()),
            "trace_id": _b64(f"trace {self.lt}"),
            "orig_status": "active",
            "end_status": "active",
            "description": {
                "type": "ord",
                "aborted": aborted,
                "destroyed": False,
                "compute_ph": {
                    "skipped": False,
                    "success": compute_success,
                    "exit_code": 0 if compute_success else 707,
                },
                "action": {"success": not aborted, "valid": True, "result_code": 0},
            },
            "in_msg": {
                "hash": _b64(f"msg {self.lt}"),
                "source": PAYER_JETTON_WALLET.raw,
                "destination": (destination or account).raw,
                "value": "49714798",
                "opcode": opcode,
                "decoded_opcode": "jetton_internal_transfer",
                "bounce": True,
                "bounced": bounced,
                "message_content": {
                    "hash": _b64(f"body {self.lt}"),
                    "body": "te6cc...",
                    "decoded": body if decoded else None,
                },
            },
            "emulated": False,
            "finality": finality,
        }
        self.transactions.append(tx)
        return tx

    def outgoing(self) -> dict[str, Any]:
        """The owner sends USDT away: a ``transfer`` on the jetton wallet, not a credit."""
        tx = self.credit(5 * USDT, None, opcode="0x0f8a7ea5")
        tx["in_msg"]["source"] = OWNER.raw
        tx["in_msg"]["message_content"]["decoded"] = {"@type": "jetton_transfer"}
        return tx

    def reads(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/api/v3/transactions"]

    def lookups(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/api/v2/runGetMethodStd"]

    # --- the API ---------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            scripted = self.script.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        if request.method == "POST" and request.url.path == "/api/v2/runGetMethodStd":
            return self._get_wallet_address(json.loads(request.content))
        if request.method == "GET" and request.url.path == "/api/v3/transactions":
            return self._transactions(request.url.params)
        return httpx.Response(404, json={"error": "not found", "code": 404})

    def _get_wallet_address(self, body: dict[str, Any]) -> httpx.Response:
        assert TonAddress.parse(body["address"]) == MASTER
        assert body["method"] == "get_wallet_address"
        [entry] = body["stack"]
        assert entry["@type"] == "tvm.stackEntrySlice"
        owner = read_boc_address(entry["slice"]["bytes"])
        wallet = JETTON_WALLET if owner == OWNER else _address(f"wallet of {owner.raw}")
        slice_ = {"@type": "tvm.slice", "bytes": _with_crc(address_cell_boc(wallet))}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "@type": "smc.runResult",
                    "gas_used": 4000,
                    "stack": [{"@type": "tvm.stackEntrySlice", "slice": slice_}],
                    "exit_code": 0,
                },
                "@extra": "1.2",
            },
        )

    def _transactions(self, params: httpx.QueryParams) -> httpx.Response:
        account = TonAddress.parse(params["account"])
        rows = [tx for tx in self.transactions if TonAddress.parse(tx["account"]) == account]
        rows += self.leaked
        if "start_lt" in params and not self.ignore_start_lt:
            rows = [tx for tx in rows if int(tx["lt"]) >= int(params["start_lt"])]
        if "start_utime" in params:
            rows = [tx for tx in rows if tx["now"] >= int(params["start_utime"])]
        rows.sort(key=lambda tx: int(tx["lt"]), reverse=params.get("sort") == "desc")
        rows = rows[: int(params.get("limit", "10"))]
        return httpx.Response(200, json={"transactions": rows, "address_book": {}})


@pytest.fixture
def api(clock: FakeClock) -> FakeToncenter:
    return FakeToncenter(clock)


@pytest.fixture
async def ton(core: Core, api: FakeToncenter, clock: FakeClock) -> AsyncIterator[TonProvider]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    provider = TonProvider(
        wallet=OWNER.friendly(bounceable=False),
        public_base_url="https://club.example",
        client=client,
        clock=clock,
    )
    core.providers.register(provider)
    yield provider
    await client.aclose()


@pytest.fixture
async def web(core: Core) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(create_ton_checkout_router(core))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://club.example") as client:
        yield client


async def checkout(core: Core, member: int = MEMBER) -> tuple[int, str]:
    link = await club.start_checkout(core, telegram_id=member, plan_code="month", provider="ton")
    return link.subscription_id, link.url.rsplit("/", 1)[1]


async def poll(core: Core, clock: FakeClock, api: FakeToncenter) -> TonReport:
    """One scheduler pass's worth of polling, after the poll interval; never two requests."""
    clock.advance(POLL)
    before = len(api.requests)
    report = await poll_ton_wallet(core)
    assert len(api.requests) - before == report.requests <= 1
    return report


async def ready(core: Core, clock: FakeClock, api: FakeToncenter) -> tuple[int, str]:
    """A member who opened a checkout, with the jetton wallet already pinned."""
    sub_id, comment = await checkout(core)
    await poll(core, clock, api)
    assert len(api.lookups()) == 1
    return sub_id, comment


async def invoice_of(core: Core, comment: str) -> TonInvoice:
    async with core.sessions() as session:
        return (
            await session.scalars(select(TonInvoice).where(TonInvoice.comment == comment))
        ).one()


async def transfers(core: Core) -> list[TonTransfer]:
    async with core.sessions() as session:
        return list((await session.scalars(select(TonTransfer).order_by(TonTransfer.id))).all())


async def alerts(core: Core) -> list[str]:
    return [n.text for n in await notifications(core, "owner_alert")]


# --- the real shape ----------------------------------------------------------------------------


def test_a_real_toncenter_credit_reads_as_a_transfer() -> None:
    [tx] = json.loads((FIXTURES / "toncenter_jetton_credit.json").read_text())["transactions"]
    wallet = TonAddress.parse(tx["account"])
    assert is_credit(tx, wallet)
    transfer = read_credit(tx)
    assert (transfer.amount, transfer.lt) == (750_000, 113976709000021)
    assert transfer.comment == "50 Telegram Stars \n\nRef#lE5lH38YB"
    assert transfer.tx_hash == "ybbYuw9CqJwkDzb3nJj24nJIP/T2chjhrGD2v/RPuXU="
    assert transfer.sender == "0:AD0EF4284DA05F50D5C08927EB8962E8EA999740A1E2E43C8640174C674D3AFF"
    assert int(transfer.occurred_at.timestamp()) == 1793606798
    # The same transaction is nobody's credit but that wallet's.
    assert not is_credit(tx, JETTON_WALLET)


@pytest.mark.parametrize(
    ("text", "normal"),
    [
        ("PC7K3M9QX2", "PC7K3M9QX2"),
        (" pc-7k3m 9qx2\n", "PC7K3M9QX2"),
        ("PC7K3MOQXl", "PC7K3M0QX1"),  # O and l typed for 0 and 1
        ("hello", "HELLO"),
        ("", None),
        ("—", None),
        (None, None),
    ],
)
def test_comments_are_matched_as_typed_by_hand(text: str | None, normal: str | None) -> None:
    assert normalize_comment(text) == normal


def test_amounts_are_written_as_wallets_take_them() -> None:
    assert format_units(10 * USDT, 6) == "10"
    assert format_units(10_500_000, 6) == "10.5"
    assert format_units(1, 6) == "0.000001"


async def test_the_master_answers_for_the_owner_s_jetton_wallet(
    api: FakeToncenter, ton: TonProvider
) -> None:
    assert await ton.resolve_jetton_wallet() == JETTON_WALLET
    [lookup] = api.lookups()
    body = json.loads(lookup.content)
    assert body["method"] == "get_wallet_address"
    assert TonAddress.parse(body["address"]) == MASTER
    assert read_boc_address(body["stack"][0]["slice"]["bytes"]) == OWNER


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"ok": True, "result": {"exit_code": 9}}),
        httpx.Response(200, json={"ok": False, "error": "LITE_SERVER_UNKNOWN", "code": 500}),
        httpx.Response(200, json={"ok": True, "result": {"exit_code": 0, "stack": []}}),
        httpx.Response(502, text="<html>Bad Gateway</html>"),
    ],
)
async def test_an_answer_without_an_address_is_an_error(
    api: FakeToncenter, ton: TonProvider, answer: httpx.Response
) -> None:
    api.script.append(answer)
    with pytest.raises(TonError):
        await ton.resolve_jetton_wallet()


@pytest.mark.parametrize(
    "attack",
    [
        "fake jetton wallet",
        "wrong destination",
        "aborted",
        "action phase failed",
        "compute failed",
        "bounced",
        "transfer notification",
        "not finalized",
    ],
)
async def test_only_successful_credits_into_the_wallet_are_read(
    api: FakeToncenter, ton: TonProvider, clock: FakeClock, attack: str
) -> None:
    since = clock.now()
    if attack == "fake jetton wallet":
        api.leaked.append(api.credit(PRICE, "PC00000001", account=FAKE_WALLET))
    elif attack == "wrong destination":
        api.credit(PRICE, "PC00000001", destination=FAKE_WALLET)
    elif attack == "aborted":
        api.credit(PRICE, "PC00000001", aborted=True, compute_success=False)
    elif attack == "action phase failed":
        api.credit(PRICE, "PC00000001", aborted=True)
    elif attack == "compute failed":
        api.credit(PRICE, "PC00000001", compute_success=False)
    elif attack == "bounced":
        api.credit(PRICE, "PC00000001", bounced=True)
    elif attack == "transfer notification":
        api.credit(PRICE, "PC00000001", opcode="0x7362d09c")
    else:
        api.credit(PRICE, "PC00000001", finality="confirmed")
    genuine = api.credit(PRICE, "PC00000002")

    batch = await ton.incoming(JETTON_WALLET, after_lt=None, since=since)

    if attack == "not finalized":
        # Reading stops before it: both are read on a later poll, once it is final.
        assert (batch.transfers, batch.last_lt) == ([], None)
    else:
        assert [t.tx_hash for t in batch.transfers] == [genuine["hash"]]
        assert batch.last_lt == int(genuine["lt"])
    [read] = api.reads()
    assert TonAddress.parse(read.url.params["account"]) == JETTON_WALLET


async def test_a_credit_toncenter_did_not_decode_is_reported_as_unreadable(
    api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    broken = api.credit(PRICE, "PC00000001", decoded=False)
    batch = await ton.incoming(JETTON_WALLET, after_lt=None, since=clock.now())
    assert (batch.transfers, batch.unreadable) == ([], [broken["hash"]])
    assert batch.last_lt == int(broken["lt"])


async def test_the_checkout_is_a_page_of_this_app_with_a_fresh_comment(ton: TonProvider) -> None:
    request = CheckoutRequest(
        subscription_id=3,
        member_telegram_id=MEMBER,
        plan_code="month",
        plan_title="1 месяц",
        amount=Money(1000, "USD"),
        for_period=1,
        description="Подписка",
    )
    first, second = await ton.start_checkout(request), await ton.start_checkout(request)
    assert first.url == f"https://club.example/ton/pay/{first.provider_payment_id}"
    assert first.provider_payment_id != second.provider_payment_id
    assert normalize_comment(first.provider_payment_id) == first.provider_payment_id
    assert ton.units(Money(1000, "USD")) == PRICE
    with pytest.raises(ValueError, match="USD"):
        ton.units(Money(99000, "RUB"))


# --- checkout ----------------------------------------------------------------------------------


async def test_a_checkout_records_an_invoice_and_links_to_its_page(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="ton")

    comment = link.url.rsplit("/", 1)[1]
    assert link.url == f"https://club.example/ton/pay/{comment}"
    assert normalize_comment(comment) == comment and comment.startswith("PC")
    assert (link.provider, link.amount) == ("ton", Money(1000, "USD"))
    invoice = await invoice_of(core, comment)
    assert (invoice.amount_units, invoice.received_units) == (PRICE, 0)
    assert (invoice.status, invoice.for_period) == (TonInvoiceStatus.OPEN, 1)
    assert invoice.expires_at == clock.now() + timedelta(minutes=60)
    assert api.requests == []  # nothing is asked of toncenter until the scheduler polls
    _, second = await checkout(core)
    assert second != comment


async def test_the_page_shows_wallet_amount_and_comment_to_copy_and_wallet_links(
    core: Core, ton: TonProvider, web: httpx.AsyncClient
) -> None:
    _, comment = await checkout(core)

    response = await web.get(f"/ton/pay/{comment.lower()}")

    assert response.status_code == 200
    page = response.text
    wallet = OWNER.friendly(bounceable=False)
    for value in (wallet, "10", comment):
        assert f'data-copy="{value}"' in page
    query = f"jetton={USDT_MASTER}&amp;amount={PRICE}&amp;text={comment}"
    assert f'href="https://app.tonkeeper.com/transfer/{wallet}?{query}"' in page
    assert f'href="ton://transfer/{wallet}?{query}"' in page
    assert '<meta http-equiv="refresh" content="20">' in page
    policy = response.headers["content-security-policy"]
    assert "script-src 'sha256-" in policy and "default-src 'none'" in policy
    assert response.headers["referrer-policy"] == "no-referrer"
    assert (await web.get("/ton/pay/PC00000000")).status_code == 404


async def test_the_page_is_gone_while_the_provider_is_off(
    core: Core, web: httpx.AsyncClient
) -> None:
    assert (await web.get("/ton/pay/PC00000000")).status_code == 404


async def test_a_raw_wallet_address_is_shown_in_the_form_wallets_take(
    core: Core, api: FakeToncenter, clock: FakeClock
) -> None:
    provider = TonProvider(wallet=OWNER.raw, public_base_url="https://x.example", clock=clock)
    try:
        assert provider.wallet_text == OWNER.friendly(bounceable=False)
        tonkeeper, deeplink = provider.transfer_links(units=5, comment="PC1")
        query = f"jetton={USDT_MASTER}&amount=5&text=PC1"
        assert deeplink == f"ton://transfer/{provider.wallet_text}?{query}"
        assert tonkeeper.startswith("https://app.tonkeeper.com/transfer/UQ")
    finally:
        await provider.aclose()


# --- pinning the jetton wallet -----------------------------------------------------------------


async def test_the_jetton_wallet_comes_from_the_master_and_is_pinned(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    await checkout(core)

    first = await poll(core, clock, api)

    assert first.requests == 1
    [lookup] = api.requests
    body = json.loads(lookup.content)
    assert body["method"] == "get_wallet_address"
    assert TonAddress.parse(body["address"]) == MASTER
    assert read_boc_address(body["stack"][0]["slice"]["bytes"]) == OWNER
    async with core.sessions() as session:
        [row] = (await session.scalars(select(TonWallet))).all()
    assert row.jetton_wallet == JETTON_WALLET.raw

    await poll(core, clock, api)
    [read] = api.reads()
    assert TonAddress.parse(read.url.params["account"]) == JETTON_WALLET

    # After a restart the pinned address is used as it is: the master is not asked again.
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        restarted = TonProvider(
            wallet=OWNER.raw, public_base_url="https://club.example", client=client, clock=clock
        )
        core.providers = ProviderRegistry([restarted])
        await poll(core, clock, api)
    assert len(api.lookups()) == 1 and len(api.reads()) == 2


async def test_an_answer_without_an_address_pins_nothing_and_alerts_the_owner(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    await checkout(core)
    api.script.append(httpx.Response(200, json={"ok": True, "result": {"exit_code": 9}}))

    report = await poll(core, clock, api)

    assert report.errors == 1
    async with core.sessions() as session:
        [row] = (await session.scalars(select(TonWallet))).all()
    assert row.jetton_wallet is None
    [alert] = await alerts(core)
    assert "toncenter" in alert
    await poll(core, clock, api)  # the next pass asks again, and pins
    async with core.sessions() as session:
        assert (await session.scalars(select(TonWallet.jetton_wallet))).one() == JETTON_WALLET.raw


# --- reading the wallet ------------------------------------------------------------------------


async def test_a_transfer_with_the_comment_activates_the_subscription(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    tx = api.credit(PRICE, comment)

    report = await poll(core, clock, api)

    assert (report.transfers, report.paid) == (1, 1)
    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid) == (SubStatus.ACTIVE, 1)
    [payment] = await payments(core)
    assert (payment.provider, payment.provider_payment_id) == ("ton", comment)
    assert (payment.amount_minor, payment.currency) == (1000, "USD")
    invoice = await invoice_of(core, comment)
    assert (invoice.status, invoice.outcome) == (TonInvoiceStatus.PAID, "applied")
    [stored] = await transfers(core)
    assert (stored.tx_hash, stored.invoice_id, stored.sender) == (tx["hash"], invoice.id, PAYER.raw)
    assert len(await notifications(core, "activated")) == 1
    async with core.sessions() as session:
        assert (await session.scalars(select(TonWallet.last_lt))).one() == int(tx["lt"])


async def test_one_indexer_call_per_pass_however_many_checkouts_wait(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    comments = [(await checkout(core, member))[1] for member in range(900, 905)]
    await poll(core, clock, api)  # pins the wallet
    for comment in comments[:3]:
        api.credit(PRICE, comment)
    api.outgoing()
    requests_before = len(api.requests)

    clock.advance(POLL)
    report = await Scheduler(core, FakeRevoker()).tick()

    assert len(api.requests) - requests_before == 1
    assert (report.ton_requests, report.ton_transfers, report.ton_paid) == (1, 3, 3)
    assert report.errors == 0
    assert len(await notifications(core, "activated")) == 3


async def test_two_workers_polling_at_once_make_one_request_and_one_payment(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(PRICE, comment)
    clock.advance(POLL)
    before = len(api.requests)

    first, second = await asyncio.gather(poll_ton_wallet(core), poll_ton_wallet(core))

    assert len(api.requests) - before == first.requests + second.requests == 1
    assert first.paid + second.paid == 1
    assert len(await payments(core)) == 1
    assert (await load_sub(core, sub_id)).periods_paid == 1


async def test_a_member_who_typed_the_comment_by_hand_is_still_found(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    typed = f" {comment[:2].lower()}-{comment[2:6]} {comment[6:].lower()} "
    api.credit(PRICE, typed, inline=True)

    await poll(core, clock, api)

    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE


@pytest.mark.parametrize(
    "attack",
    [
        "fake jetton wallet",
        "wrong destination",
        "aborted",
        "action phase failed",
        "compute failed",
        "bounced",
        "transfer notification",
    ],
)
async def test_only_successful_credits_into_the_pinned_wallet_count(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock, attack: str
) -> None:
    sub_id, comment = await ready(core, clock, api)
    if attack == "fake jetton wallet":
        # A contract that claims the USDT master "credits" itself, flawlessly, and the indexer
        # hands that back with our wallet's history. It did not run on our wallet.
        api.leaked.append(api.credit(PRICE, comment, account=FAKE_WALLET))
    elif attack == "wrong destination":
        api.credit(PRICE, comment, destination=FAKE_WALLET)
    elif attack == "aborted":
        # A forged internal_transfer to our real wallet: the genuine code rejects it.
        api.credit(PRICE, comment, aborted=True, compute_success=False)
    elif attack == "action phase failed":
        # The code ran, but the transaction was rolled back: nothing was credited.
        api.credit(PRICE, comment, aborted=True)
    elif attack == "compute failed":
        api.credit(PRICE, comment, compute_success=False)
    elif attack == "bounced":
        api.credit(PRICE, comment, bounced=True)
    else:
        # What naive watchers trust: a notification-looking message. It never counts.
        api.credit(PRICE, comment, opcode="0x7362d09c")

    report = await poll(core, clock, api)

    assert (report.transfers, report.paid) == (0, 0)
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    assert await payments(core) == [] and await transfers(core) == []
    assert (await invoice_of(core, comment)).received_units == 0
    # Our own transactions are read past; another account's does not move the cursor.
    async with core.sessions() as session:
        cursor = (await session.scalars(select(TonWallet.last_lt))).one()
    assert cursor == (None if attack == "fake jetton wallet" else api.lt)


async def test_the_jetton_master_a_transfer_claims_is_never_asked_for(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    """Only two kinds of request exist: the master's own answer, and our wallet's history."""
    sub_id, comment = await ready(core, clock, api)
    api.credit(PRICE, comment, account=FAKE_WALLET)
    for _ in range(3):
        await poll(core, clock, api)
    paths = {(r.method, r.url.path) for r in api.requests}
    assert paths == {("POST", "/api/v2/runGetMethodStd"), ("GET", "/api/v3/transactions")}
    assert {TonAddress.parse(r.url.params["account"]) for r in api.reads()} == {JETTON_WALLET}
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING


async def test_the_same_transfer_counts_once_across_passes_and_restarts(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(6 * USDT, comment)
    await poll(core, clock, api)
    # A worker died before it saved how far it had read, and the indexer ignores start_lt.
    async with core.sessions() as session, session.begin():
        await session.execute(update(TonWallet).values(last_lt=None))
    api.ignore_start_lt = True

    for _ in range(2):
        report = await poll(core, clock, api)
        assert (report.transfers, report.paid) == (0, 0)

    assert len(await transfers(core)) == 1
    assert (await invoice_of(core, comment)).received_units == 6 * USDT
    assert len(await notifications(core, "ton_underpaid")) == 1
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING


async def test_a_read_returns_only_what_comes_after_the_cursor_even_if_the_indexer_repeats(
    api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    old = api.credit(PRICE, "PC00000001")
    new = api.credit(PRICE, "PC00000002")
    api.ignore_start_lt = True

    batch = await ton.incoming(JETTON_WALLET, after_lt=int(old["lt"]), since=clock.now())

    assert [t.tx_hash for t in batch.transfers] == [new["hash"]]
    assert batch.last_lt == int(new["lt"])
    assert api.reads()[0].url.params["start_lt"] == str(int(old["lt"]) + 1)


async def test_an_underpayment_waits_for_a_top_up_with_the_same_comment(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(6 * USDT, comment)

    await poll(core, clock, api)

    invoice = await invoice_of(core, comment)
    assert (invoice.status, invoice.received_units) == (TonInvoiceStatus.OPEN, 6 * USDT)
    assert invoice.expires_at == clock.now() + timedelta(minutes=60)
    [message] = await notifications(core, "ton_underpaid")
    assert message.text.startswith("Пришло 6 USDT из 10 USDT. Осталось доплатить 4 USDT")
    assert comment in message.text
    assert message.buttons == [
        {"text": "Открыть оплату", "url": f"https://club.example/ton/pay/{comment}"}
    ]
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING

    api.credit(4 * USDT, comment)
    report = await poll(core, clock, api)

    assert report.paid == 1
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE
    [payment] = await payments(core)
    assert payment.amount_minor == 1000
    assert await alerts(core) == []


async def test_the_page_tells_what_is_left_after_a_partial_payment(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock, web: httpx.AsyncClient
) -> None:
    _, comment = await ready(core, clock, api)
    api.credit(6_500_000, comment)
    await poll(core, clock, api)

    page = (await web.get(f"/ton/pay/{comment}")).text

    assert "Пришло 6,5 USDT из 10 USDT." in page
    assert 'data-copy="3.5"' in page
    assert f"amount=3500000&amp;text={comment}" in page


async def test_an_overpayment_is_accepted_and_the_owner_told_about_the_surplus(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock, web: httpx.AsyncClient
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(12_500_000, comment)

    await poll(core, clock, api)

    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE
    [payment] = await payments(core)
    assert payment.amount_minor == 1000  # the price is what pays the period
    [alert] = await alerts(core)
    assert "переплатой: пришло 12,5 USDT вместо 10 USDT" in alert
    assert "2,5 USDT" in alert
    page = (await web.get(f"/ton/pay/{comment}")).text
    assert "Оплата получена." in page
    assert "http-equiv" not in page


async def test_a_fraction_of_a_cent_over_the_price_is_only_logged(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(PRICE + 9_999, comment)
    await poll(core, clock, api)
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE
    assert await alerts(core) == []


async def test_a_transfer_after_the_checkout_expired_still_counts_and_the_owner_is_told(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock, web: httpx.AsyncClient
) -> None:
    sub_id, comment = await ready(core, clock, api)
    clock.advance(timedelta(minutes=61))
    await poll(core, clock, api)
    assert (await invoice_of(core, comment)).status is TonInvoiceStatus.EXPIRED
    assert "Время на оплату вышло" in (await web.get(f"/ton/pay/{comment}")).text

    api.credit(PRICE, comment)
    clock.advance(LATE_POLL)  # nothing is open: the wallet is read every five minutes
    report = await poll(core, clock, api)

    assert report.paid == 1
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE
    [alert] = await alerts(core)
    assert "пришёл после того, как счёт истёк. Он засчитан" in alert


async def test_a_partly_paid_checkout_that_expires_is_reported_and_can_still_be_completed(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(4 * USDT, comment)
    await poll(core, clock, api)
    clock.advance(timedelta(minutes=61))

    await poll(core, clock, api)

    assert (await invoice_of(core, comment)).status is TonInvoiceStatus.EXPIRED
    [owner] = await alerts(core)
    assert "истёк оплаченным не полностью: пришло 4 USDT из 10 USDT" in owner
    [member] = await notifications(core, "ton_partial_expired")
    assert "пришло только 4 USDT из 10 USDT. Доплатить 6 USDT" in member.text

    api.credit(6 * USDT, comment)
    clock.advance(LATE_POLL)
    await poll(core, clock, api)
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE
    assert len(await alerts(core)) == 2  # and the late top-up was reported too


async def test_nothing_is_asked_while_no_checkout_waits(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    assert (await poll(core, clock, api)).requests == 0
    await ready(core, clock, api)
    clock.advance(timedelta(minutes=61) + LATE_WATCH)
    assert (await poll(core, clock, api)).requests == 0
    assert (await Scheduler(core, FakeRevoker()).tick()).ton_requests == 0


async def test_money_no_checkout_expects_is_reported_to_the_owner(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    await ready(core, clock, api)
    no_comment = api.credit(3 * USDT, None)
    api.credit(2 * USDT, "for the club, from Anna")

    await poll(core, clock, api)

    first, second = await alerts(core)
    assert "3 USDT без комментария" in first and no_comment["hash"] in first
    assert "2 USDT с комментарием «for the club, from Anna»" in second
    assert PAYER.raw in second
    stored = await transfers(core)
    assert [row.invoice_id for row in stored] == [None, None]


async def test_money_for_a_paid_checkout_is_reported_to_the_owner(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    _, comment = await ready(core, clock, api)
    api.credit(PRICE, comment)
    await poll(core, clock, api)
    extra = api.credit(PRICE, comment)

    # Nothing is open now: the wallet is still watched, only less often.
    assert (await poll(core, clock, api)).requests == 0
    clock.advance(LATE_POLL)
    assert (await poll(core, clock, api)).requests == 1

    [alert] = await alerts(core)
    assert "уже оплачен, а пришло ещё 10 USDT" in alert and extra["hash"] in alert
    assert len(await payments(core)) == 1


async def test_a_second_checkout_paid_for_the_same_period_is_flagged_for_a_refund(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock, web: httpx.AsyncClient
) -> None:
    sub_id, first = await ready(core, clock, api)
    api.credit(PRICE, first)
    await poll(core, clock, api)
    clock.advance(timedelta(days=27))
    second = (await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)).url
    third = (await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)).url
    for url in (second, third):
        api.credit(PRICE, url.rsplit("/", 1)[1])

    await poll(core, clock, api)

    sub = await load_sub(core, sub_id)
    assert sub.periods_paid == 2
    outcomes = [p.outcome for p in await payments(core)]
    assert outcomes == [PaymentOutcome.APPLIED, PaymentOutcome.APPLIED, PaymentOutcome.NEEDS_REFUND]
    page = (await web.get(f"/ton/pay/{third.rsplit('/', 1)[1]}")).text
    assert "не зачислен к подписке" in page


async def test_an_unreadable_credit_is_reported_and_the_others_still_count(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    broken = api.credit(PRICE, comment, decoded=False)
    api.credit(PRICE, comment)

    await poll(core, clock, api)

    [alert] = await alerts(core)
    assert broken["hash"] in alert
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE


async def test_a_transaction_not_finalized_yet_is_read_on_a_later_pass(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    tx = api.credit(PRICE, comment, finality="confirmed")

    await poll(core, clock, api)
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    async with core.sessions() as session:
        assert (await session.scalars(select(TonWallet.last_lt))).one() is None

    tx["finality"] = 2  # toncenter's spec writes finality as an integer
    await poll(core, clock, api)
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE


async def test_toncenter_errors_are_retried_and_the_owner_told_once_a_day(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(PRICE, comment)
    api.script += [
        httpx.Response(429, json={"error": "Ratelimit exceed", "code": 429}),
        httpx.ConnectError("refused"),
    ]

    assert (await poll(core, clock, api)).errors == 1
    assert (await poll(core, clock, api)).errors == 1
    [alert] = await alerts(core)
    assert "Ratelimit" in alert

    await poll(core, clock, api)
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE


async def test_the_api_key_goes_in_a_header(
    core: Core, api: FakeToncenter, clock: FakeClock
) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    provider = TonProvider(
        wallet=OWNER.raw,
        public_base_url="https://club.example",
        api_key="k-123",
        api_url="https://testnet.toncenter.com/api/",
        client=client,
        clock=clock,
    )
    try:
        await provider.resolve_jetton_wallet()
    finally:
        await client.aclose()
    [request] = api.requests
    assert request.headers["X-API-Key"] == "k-123"
    assert str(request.url) == "https://testnet.toncenter.com/api/v2/runGetMethodStd"
    assert provider.wallet_text == OWNER.friendly(bounceable=False, testnet=True)


# --- the rest of the provider contract ---------------------------------------------------------


async def test_renewals_have_nothing_to_charge_and_nothing_to_look_up(
    ton: TonProvider, api: FakeToncenter
) -> None:
    key = renewal_idempotence_key(7, 2, 0)
    request = RenewalRequest(
        subscription_id=7,
        member_telegram_id=MEMBER,
        plan_code="month",
        amount=Money(1000, "USD"),
        for_period=2,
        attempt=0,
        payment_method_id=None,
        idempotence_key=key,
        description="Продление",
    )
    result = await ton.charge_renewal(request)
    assert isinstance(result, PaymentFailed)
    assert result.reason == "no_saved_payment_method"
    assert await ton.check_payment("PC00000000") is None
    with pytest.raises(WebhookRejected):
        await ton.parse_webhook({}, b"{}")
    assert api.requests == []


async def test_the_next_period_is_a_new_checkout_that_starts_where_the_last_one_ends(
    core: Core, api: FakeToncenter, ton: TonProvider, clock: FakeClock
) -> None:
    sub_id, comment = await ready(core, clock, api)
    api.credit(PRICE, comment)
    await poll(core, clock, api)
    end = (await load_sub(core, sub_id)).current_period_end
    assert end is not None

    clock.set(end)
    report = await Scheduler(core, FakeRevoker()).tick()
    assert report.charge_failed == 1
    [unpaid] = await notifications(core, "payment_failed")
    assert unpaid.text.startswith("Оплаченный период закончился, а следующий не оплачен")

    clock.advance(timedelta(hours=5))
    link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    api.credit(PRICE, link.url.rsplit("/", 1)[1])
    await poll(core, clock, api)

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.periods_paid, sub.current_period_start) == (
        SubStatus.ACTIVE,
        2,
        end,
    )
