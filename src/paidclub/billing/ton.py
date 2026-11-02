"""USDT on TON, paid straight to the owner's own wallet: no payment service in between.

Nothing announces these payments, so the app watches the wallet itself through toncenter
(https://toncenter.com/api/v3/doc.json for the indexer, /api/v2/openapi.json for get-methods):

* **Checkout.** A page of this app, ``/ton/pay/<comment>`` (:mod:`paidclub.web.ton_checkout`),
  with the owner's wallet, the exact amount of USDT and a comment unique to the checkout,
  each to copy, plus a Tonkeeper link and a ``ton://transfer/...?jetton=...&amount=...&text=``
  link. Telegram buttons cannot open ``ton://``, so the button opens the page.
* **Whose money counts.** A jetton is credited by an ``internal_transfer`` message to the
  recipient's *jetton wallet*, a contract derived from the jetton master and the owner. Only
  transfers into the owner's USDT jetton wallet are accepted, and that address is taken from
  the master itself: ``get_wallet_address(owner)`` (``POST /v2/runGetMethodStd``), once, then
  pinned in the database (:class:`paidclub.models.TonWallet`). A fake jetton wallet can claim
  any master in what an indexer reports about it, but it cannot credit that address: the
  genuine wallet code there accepts ``internal_transfer`` only from the master or from another
  genuine wallet, and aborts the transaction otherwise. The ``jetton_master`` field of a
  transfer is never read.
* **Finding payments.** One request per poll: ``GET /v3/transactions?account=<jetton wallet>``
  from the last logical time read, oldest first. toncenter decodes the incoming message, so
  the amount and the text comment come from the crediting transaction itself. A transaction
  counts only if it ran on that wallet, its message was addressed to it, was an
  ``internal_transfer`` (``0x178d4519``) and not a bounce, it is finalized, and it was not
  aborted. The transaction hash identifies it, so a transfer seen twice counts once.
* **No renewals,** as with Crypto Pay: every period is a new checkout, so ``charge_renewal``
  reports ``no_saved_payment_method`` without a request.

The provider only talks to toncenter. Matching transfers to checkouts, adding them up and
applying them is :mod:`paidclub.ton_payments`.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self
from urllib.parse import quote

import httpx

from paidclub.billing.base import (
    NO_SAVED_METHOD,
    ChargeResult,
    Checkout,
    CheckoutRequest,
    PaymentFailed,
    PaymentPurpose,
    ProviderError,
    ProviderSubscriptionRef,
    RenewalRequest,
    WebhookEvent,
    WebhookRejected,
)
from paidclub.billing.tonaddress import (
    BocError,
    InvalidAddress,
    TonAddress,
    address_cell_boc,
    read_boc_address,
)
from paidclub.clock import Clock, SystemClock
from paidclub.config import Settings
from paidclub.money import Money

log = logging.getLogger(__name__)

PROVIDER_NAME = "ton"
API_URL = "https://toncenter.com/api"
TESTNET_API_URL = "https://testnet.toncenter.com/api"
#: The official USDT (Tether USD) jetton master on the TON mainnet.
USDT_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
USDT_DECIMALS = 6
#: ``internal_transfer`` from TEP-74: the message that credits a jetton wallet.
OP_INTERNAL_TRANSFER = 0x178D4519
#: Transactions read per poll; a busier wallet is caught up over the next passes.
BATCH = 50
#: Path of the payment page; the comment follows.
PAGE_PATH = "/ton/pay"
#: Comments are ``PC`` and 8 characters of Crockford's base32: 40 random bits, easy to type.
COMMENT_PREFIX = "PC"
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: How a typed comment is read back: letters people confuse with digits count as the digits.
_TYPOS = str.maketrans({"O": "0", "I": "1", "L": "1"})
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class TonError(ProviderError):
    """toncenter could not be reached, refused, or answered with something unexpected."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class IncomingTransfer:
    """A jetton credit to the owner's jetton wallet, from a transaction that passed the checks."""

    #: Hash of the crediting transaction (base64, as toncenter writes it).
    tx_hash: str
    lt: int
    #: In the jetton's smallest units.
    amount: int
    comment: str | None
    #: The payer's wallet, raw form, as the sending jetton wallet put it in the message.
    sender: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class IncomingBatch:
    transfers: list[IncomingTransfer]
    #: Credits whose message could not be read: they reached the wallet, so the owner is told.
    unreadable: list[str]
    #: The logical time of the last transaction read; ``None`` if none was.
    last_lt: int | None


def new_comment() -> str:
    return COMMENT_PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(8))


def normalize_comment(text: str | None) -> str | None:
    """A comment as matched: upper case, letters and digits only, ``O``/``I``/``L`` as digits.
    ``None`` when nothing of the kind is left."""
    if not text:
        return None
    kept = "".join(char for char in text.upper() if char.isascii() and char.isalnum())
    if kept.startswith(COMMENT_PREFIX):
        kept = COMMENT_PREFIX + kept[len(COMMENT_PREFIX) :].translate(_TYPOS)
    return kept if 0 < len(kept) <= 32 else None


def format_units(units: int, decimals: int) -> str:
    """``10500000`` with 6 decimals -> ``"10.5"``: what a wallet app expects to be typed."""
    return f"{Decimal(units).scaleb(-decimals).normalize():f}"


def _shown(text: str, address: TonAddress, *, bounceable: bool, testnet: bool) -> str:
    """An address as the owner wrote it if user-friendly; a raw one is shown user-friendly,
    which is what wallet apps and links take."""
    value = text.strip()
    return address.friendly(bounceable=bounceable, testnet=testnet) if ":" in value else value


def _opcode(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def _is_final(tx: Mapping[str, Any]) -> bool:
    """Committed for good. toncenter writes ``"finalized"`` (its spec says the integer 2)."""
    if tx.get("emulated") is True:
        return False
    finality = tx.get("finality")
    return finality is None or finality in ("finalized", 2)


def _same(address: Any, expected: TonAddress) -> bool:
    if not isinstance(address, str):
        return False
    try:
        return TonAddress.parse(address) == expected
    except InvalidAddress:
        return False


def _comment(payload: Any) -> str | None:
    """The text comment of a decoded ``forward_payload``, inline or in a reference."""
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("value", payload)
    if isinstance(value, Mapping) and value.get("@type") == "text_comment":
        text = value.get("text", value.get("comment"))
        return text if isinstance(text, str) else None
    return None


def _sender(decoded: Mapping[str, Any]) -> str | None:
    source = decoded.get("from")
    if not isinstance(source, Mapping) or source.get("@type") != "addr_std":
        return None
    try:
        workchain = int(source["workchain_id"])
        return TonAddress(workchain, bytes.fromhex(str(source["address"]))).raw
    except (KeyError, ValueError, InvalidAddress):
        return None


def is_credit(tx: Mapping[str, Any], wallet: TonAddress) -> bool:
    """Whether ``tx`` is a successful ``internal_transfer`` into ``wallet``: the only kind of
    transaction that can mean the owner received the jetton. Everything here is read from the
    receiving side; nothing the sender or an indexer says about a jetton master is used."""
    message = tx.get("in_msg")
    description = tx.get("description")
    if not isinstance(message, Mapping) or not isinstance(description, Mapping):
        return False
    compute = description.get("compute_ph")
    return (
        _same(tx.get("account"), wallet)
        and _same(message.get("destination"), wallet)
        and message.get("bounced") is not True
        and _opcode(message.get("opcode")) == OP_INTERNAL_TRANSFER
        and description.get("aborted") is False
        and isinstance(compute, Mapping)
        and compute.get("success") is True
    )


def read_credit(tx: Mapping[str, Any]) -> IncomingTransfer:
    """The transfer in a transaction :func:`is_credit` accepted. Raises
    ``KeyError``/``TypeError``/``ValueError`` when toncenter did not decode its message."""
    content = tx["in_msg"]["message_content"]
    decoded = content["decoded"]
    if decoded["@type"] != "jetton_internal_transfer":
        raise ValueError(f"decoded as {decoded['@type']!r}")
    amount = decoded["amount"]
    units = int(amount["value"] if isinstance(amount, Mapping) else amount)
    if units <= 0:
        raise ValueError("no amount")
    return IncomingTransfer(
        tx_hash=str(tx["hash"]),
        lt=int(tx["lt"]),
        amount=units,
        comment=_comment(decoded.get("forward_payload")),
        sender=_sender(decoded),
        occurred_at=datetime.fromtimestamp(int(tx["now"]), UTC),
    )


class TonProvider:
    name = PROVIDER_NAME
    currency = "USD"
    #: As with Crypto Pay: renewals are ours to start, and there is never anything to charge.
    supports_merchant_renewal = True

    def __init__(
        self,
        *,
        wallet: str,
        public_base_url: str,
        jetton_master: str = USDT_MASTER,
        decimals: int = USDT_DECIMALS,
        api_url: str = API_URL,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        if decimals < 2:
            raise ValueError("prices are in cents, so the jetton needs at least 2 decimals")
        testnet = "testnet" in api_url
        self.wallet = TonAddress.parse(wallet)
        self.jetton_master = TonAddress.parse(jetton_master)
        #: Shown on the page and put in the links. A wallet takes payments non-bounceable.
        self.wallet_text = _shown(wallet, self.wallet, bounceable=False, testnet=testnet)
        self.jetton_master_text = _shown(
            jetton_master, self.jetton_master, bounceable=True, testnet=testnet
        )
        self.decimals = decimals
        self._public_base_url = public_base_url.rstrip("/")
        self._api_url = api_url.rstrip("/")
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._clock = clock or SystemClock()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> Self:
        if settings.ton_wallet_address is None:
            raise ValueError("set PAIDCLUB_TON_WALLET_ADDRESS")
        return cls(
            wallet=settings.ton_wallet_address,
            public_base_url=settings.public_base_url,
            jetton_master=settings.ton_jetton_master,
            decimals=settings.ton_jetton_decimals,
            api_url=settings.ton_api_url,
            api_key=settings.ton_api_key.get_secret_value() if settings.ton_api_key else None,
            client=client,
            clock=clock,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- amounts and links -----------------------------------------------------------------

    @property
    def units_per_cent(self) -> int:
        return int(10 ** (self.decimals - 2))

    def units(self, amount: Money) -> int:
        if amount.currency != "USD":
            raise ValueError(f"USDT is priced in USD here, got {amount.currency}")
        return amount.minor * self.units_per_cent

    def page_url(self, comment: str) -> str:
        return f"{self._public_base_url}{PAGE_PATH}/{comment}"

    def transfer_links(self, *, units: int, comment: str) -> tuple[str, str]:
        """The Tonkeeper link and the ``ton://`` link for paying ``units`` with ``comment``."""
        query = f"jetton={self.jetton_master_text}&amount={units}&text={quote(comment, safe='')}"
        target = quote(self.wallet_text, safe="-_")
        return (
            f"https://app.tonkeeper.com/transfer/{target}?{query}",
            f"ton://transfer/{target}?{query}",
        )

    # --- HTTP ------------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        where = f"toncenter {method} {path}"
        try:
            response = await self._client.request(
                method,
                f"{self._api_url}{path}",
                params=params,
                json=body,
                headers=self._headers,
            )
        except httpx.TimeoutException as exc:
            raise TonError(f"{where}: timed out") from exc
        except httpx.HTTPError as exc:
            raise TonError(f"{where}: {type(exc).__name__}: {exc}") from exc
        try:
            data = json.loads(response.content)
        except ValueError:
            data = None
        if not response.is_success or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else response.text[:200]
            raise TonError(
                f"{where}: HTTP {response.status_code} {error}", status=response.status_code
            )
        return data

    async def resolve_jetton_wallet(self) -> TonAddress:
        """The owner's jetton wallet as the master computes it: ``get_wallet_address``."""
        data = await self._request(
            "POST",
            "/v2/runGetMethodStd",
            body={
                "address": self.jetton_master.raw,
                "method": "get_wallet_address",
                "stack": [
                    {
                        "@type": "tvm.stackEntrySlice",
                        "slice": {"@type": "tvm.slice", "bytes": address_cell_boc(self.wallet)},
                    }
                ],
            },
        )
        result = data.get("result")
        if data.get("ok") is not True or not isinstance(result, Mapping):
            raise TonError(f"get_wallet_address failed: {data.get('error') or data}")
        if result.get("exit_code") not in (0, 1):
            raise TonError(f"get_wallet_address exited with {result.get('exit_code')}")
        stack = result.get("stack")
        try:
            [entry] = stack if isinstance(stack, list) else []
            box = entry.get("slice") or entry.get("cell")
            address = read_boc_address(str(box["bytes"]))
        except (ValueError, KeyError, TypeError, AttributeError, BocError) as exc:
            raise TonError(f"get_wallet_address answered with no address: {stack!r}") from exc
        log.info(
            "USDT jetton wallet of %s is %s", self.wallet.raw, address.friendly(bounceable=True)
        )
        return address

    async def incoming(
        self, jetton_wallet: TonAddress, *, after_lt: int | None, since: datetime
    ) -> IncomingBatch:
        """Credits to ``jetton_wallet`` after ``after_lt`` (or, before anything was read, after
        ``since``), oldest first, in one request. Reading stops before a transaction that is not
        finalized yet, so the next poll starts there."""
        params = {"account": jetton_wallet.raw, "sort": "asc", "limit": str(BATCH)}
        if after_lt is not None:
            params["start_lt"] = str(after_lt + 1)
        else:
            params["start_utime"] = str(int(since.timestamp()))
        data = await self._request("GET", "/v3/transactions", params=params)
        rows = data.get("transactions")
        if not isinstance(rows, list):
            raise TonError("toncenter GET /v3/transactions: the answer is not a list")
        transfers: list[IncomingTransfer] = []
        unreadable: list[str] = []
        last_lt = after_lt
        for tx in rows:
            if not isinstance(tx, Mapping):
                break
            if not _same(tx.get("account"), jetton_wallet):
                # Not ours: neither counted nor allowed to move where the next read starts.
                log.warning("toncenter returned %s of %s", tx.get("hash"), tx.get("account"))
                continue
            if not _is_final(tx):
                break
            lt = int(tx["lt"])
            if last_lt is not None and lt <= last_lt:
                continue  # already read; an indexer that ignores start_lt must not repeat it
            last_lt = lt
            if not is_credit(tx, jetton_wallet):
                continue
            try:
                transfers.append(read_credit(tx))
            except (KeyError, TypeError, ValueError) as exc:
                log.error("credit %s to %s not readable: %s", tx.get("hash"), jetton_wallet, exc)
                unreadable.append(str(tx.get("hash")))
        return IncomingBatch(transfers=transfers, unreadable=unreadable, last_lt=last_lt)

    # --- BillingProvider -------------------------------------------------------------------

    async def start_checkout(self, request: CheckoutRequest) -> Checkout:
        """A page with the wallet, the amount and a fresh comment. Core records the invoice
        (:func:`paidclub.ton_payments.open_invoice`) before the member gets the link."""
        self.units(request.amount)  # refuses a price that is not in dollars
        comment = new_comment()
        return Checkout(url=self.page_url(comment), provider_payment_id=comment)

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        """There is nothing to charge: the period end becomes a renewal the member pays."""
        return PaymentFailed(
            provider=self.name,
            provider_payment_id=f"no_method:{request.idempotence_key}",
            subscription_id=request.subscription_id,
            amount=request.amount,
            occurred_at=self._clock.now(),
            purpose=PaymentPurpose.RENEWAL,
            reason=NO_SAVED_METHOD,
            for_period=request.for_period,
            attempt=request.attempt,
        )

    async def check_payment(self, provider_payment_id: str) -> ChargeResult | None:
        """One payment cannot be asked about: the wallet poll finds them all at once."""
        return None

    async def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Sequence[WebhookEvent]:
        raise WebhookRejected("TON payments are found by watching the wallet, not by webhooks")

    async def cancel(self, ref: ProviderSubscriptionRef) -> None:
        """Nothing to do: there is no subscription on chain, every period is a new transfer."""

    async def resume(self, ref: ProviderSubscriptionRef) -> None:
        """Nothing to do, see :meth:`cancel`."""
