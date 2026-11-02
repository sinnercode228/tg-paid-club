"""The TON provider on its own: the master's answer for the owner's jetton wallet, and which
transactions on that wallet are credits. toncenter is an in-memory fake whose transactions have
the shape of real ones (tests/fixtures/toncenter_jetton_credit.json is a real one, trimmed)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

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
from paidclub.domain.subscriptions import renewal_idempotence_key
from paidclub.money import Money

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


# --- checkout ----------------------------------------------------------------------------------


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


# --- reading the wallet ------------------------------------------------------------------------


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
