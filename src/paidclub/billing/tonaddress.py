"""TON addresses, and the little of the bag-of-cells format the TON provider needs.

An address is a workchain and a 256-bit account id, written two ways:

* raw: ``0:B113A994...621DFE``, the workchain, a colon and 64 hex digits. toncenter answers
  with this form.
* user-friendly: 48 characters of base64url (or base64) over 36 bytes: a tag (``0x11``
  bounceable, ``0x51`` non-bounceable, ``+0x80`` on the testnet), the workchain as a signed
  byte, the account id, and a CRC16-XMODEM of those 34 bytes. Wallet apps show this form.

``EQ...``, ``UQ...`` and ``0:...`` can all name the same account, so addresses are compared as
:class:`TonAddress` values, never as strings.

A bag of cells (BOC) is how TON serialises cell trees. :func:`address_cell_boc` writes the
one-cell BOC that holds an address, the argument of a jetton master's ``get_wallet_address``,
and :func:`read_boc_address` reads an address from the root cell of a BOC, which is what that
get-method returns. :func:`parse_boc` reads ordinary cells and checks the CRC32-C when the BOC
carries one; cells with stored hashes are refused rather than guessed at.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

#: Tags of the user-friendly form.
BOUNCEABLE = 0x11
NON_BOUNCEABLE = 0x51
TESTNET_FLAG = 0x80

_RAW = re.compile(r"^(-?[0-9]{1,3}):([0-9a-fA-F]{64})$")
_FRIENDLY = re.compile(r"^[A-Za-z0-9+/_-]{48}$")
_BOC_MAGIC = bytes.fromhex("b5ee9c72")


class InvalidAddress(ValueError):
    pass


class BocError(ValueError):
    """Not a bag of cells this module can read."""


def crc16(data: bytes) -> int:
    """CRC-16/XMODEM (polynomial 0x1021, initial value 0), the user-friendly checksum."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), the optional checksum at the end of a BOC."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class TonAddress:
    workchain: int
    account: bytes

    def __post_init__(self) -> None:
        if not -128 <= self.workchain <= 127:
            raise InvalidAddress(f"workchain {self.workchain} does not fit a byte")
        if len(self.account) != 32:
            raise InvalidAddress("an account id is 32 bytes")

    @classmethod
    def parse(cls, text: str) -> TonAddress:
        """Read a raw or user-friendly address; raises :class:`InvalidAddress`."""
        value = text.strip()
        raw = _RAW.match(value)
        if raw:
            return cls(int(raw[1]), bytes.fromhex(raw[2]))
        if not _FRIENDLY.match(value):
            raise InvalidAddress(f"not a TON address: {text!r}")
        data = base64.urlsafe_b64decode(value.replace("+", "-").replace("/", "_"))
        if data[0] & ~TESTNET_FLAG not in (BOUNCEABLE, NON_BOUNCEABLE):
            raise InvalidAddress(f"unknown address tag in {text!r}")
        if crc16(data[:34]) != int.from_bytes(data[34:], "big"):
            raise InvalidAddress(f"wrong checksum in {text!r}")
        return cls(int.from_bytes(data[1:2], "big", signed=True), data[2:34])

    @property
    def raw(self) -> str:
        """``0:B113...``, upper case, as toncenter writes it."""
        return f"{self.workchain}:{self.account.hex().upper()}"

    def friendly(self, *, bounceable: bool = True, testnet: bool = False) -> str:
        tag = (BOUNCEABLE if bounceable else NON_BOUNCEABLE) | (TESTNET_FLAG if testnet else 0)
        body = bytes([tag]) + self.workchain.to_bytes(1, "big", signed=True) + self.account
        return base64.urlsafe_b64encode(body + crc16(body).to_bytes(2, "big")).decode("ascii")

    def __str__(self) -> str:
        return self.raw


# --- bags of cells -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cell:
    #: The data bits as a string of ``0`` and ``1``, without the completion tag.
    bits: str
    #: Indexes of the referenced cells in the BOC.
    refs: tuple[int, ...]


def _address_bits(address: TonAddress) -> str:
    """``addr_std$10 anycast:(Maybe Anycast) workchain_id:int8 address:bits256``, no anycast."""
    workchain = format(address.workchain & 0xFF, "08b")
    return "100" + workchain + format(int.from_bytes(address.account, "big"), "0256b")


def _cell_data(bits: str) -> tuple[int, bytes]:
    """The ``d2`` descriptor and the data bytes, with the completion tag when not whole bytes."""
    d2 = (len(bits) + 7) // 8 + len(bits) // 8
    if len(bits) % 8:
        bits += "1" + "0" * (7 - len(bits) % 8)
    return d2, int(bits, 2).to_bytes(len(bits) // 8, "big") if bits else b""


def address_cell_boc(address: TonAddress) -> str:
    """Base64 of a BOC with one cell holding ``address`` (267 bits, no refs, no CRC)."""
    d2, data = _cell_data(_address_bits(address))
    cell = bytes([0, d2]) + data  # d1 = 0: no refs, ordinary cell, level 0
    # No index, no CRC; 1-byte cell indexes and offsets; one cell, one root, none absent.
    header = _BOC_MAGIC + bytes([0x01, 0x01, 1, 1, 0, len(cell), 0])
    return base64.b64encode(header + cell).decode("ascii")


def parse_boc(data: bytes) -> tuple[list[Cell], list[int]]:
    """The cells of a BOC and the indexes of its roots."""
    if data[:4] != _BOC_MAGIC or len(data) < 7:
        raise BocError("not a bag of cells")
    flags = data[4]
    has_index, has_crc, size, offset_size = flags & 0x80, flags & 0x40, flags & 0x07, data[5]
    if not 1 <= size <= 4 or not 1 <= offset_size <= 8:
        raise BocError("bad bag-of-cells header")
    position = 6

    def take(length: int) -> int:
        nonlocal position
        if position + length > len(data):
            raise BocError("truncated bag of cells")
        value = int.from_bytes(data[position : position + length], "big")
        position += length
        return value

    cell_count, root_count, absent = take(size), take(size), take(size)
    total = take(offset_size)
    if absent:
        raise BocError("absent cells are not supported")
    roots = [take(size) for _ in range(root_count)]
    if has_index:
        position += cell_count * offset_size
    body, body_end = data[position : position + total], position + total
    if has_crc:
        if body_end + 4 != len(data):
            raise BocError("truncated bag of cells")
        if crc32c(data[:body_end]) != int.from_bytes(data[body_end:], "little"):
            raise BocError("bag-of-cells checksum mismatch")
    elif body_end != len(data):
        raise BocError("trailing bytes after the cells")

    cells: list[Cell] = []
    at = 0
    for _ in range(cell_count):
        if at + 2 > len(body):
            raise BocError("truncated cell")
        d1, d2 = body[at], body[at + 1]
        if d1 & 0x10:
            raise BocError("cells with stored hashes are not supported")
        ref_count, length = d1 & 0x07, (d2 + 1) // 2
        raw = body[at + 2 : at + 2 + length]
        at += 2 + length
        refs = []
        for _ in range(ref_count):
            refs.append(int.from_bytes(body[at : at + size], "big"))
            at += size
        bits = "".join(format(byte, "08b") for byte in raw)
        if d2 % 2:  # the last byte ends with the completion tag: a 1 and trailing zeros
            bits = bits.rstrip("0")[:-1]
        cells.append(Cell(bits=bits, refs=tuple(refs)))
    if at != len(body) or any(root >= cell_count for root in roots):
        raise BocError("malformed cells")
    return cells, roots


def read_address_bits(bits: str) -> TonAddress:
    """The ``addr_std`` at the start of ``bits``; anything else raises :class:`BocError`."""
    if len(bits) < 267 or bits[:2] != "10":
        raise BocError("not a standard address")
    if bits[2] != "0":
        raise BocError("anycast addresses are not supported")
    workchain = int(bits[3:11], 2)
    if workchain > 127:
        workchain -= 256
    return TonAddress(workchain, int(bits[11:267], 2).to_bytes(32, "big"))


def read_boc_address(boc: str) -> TonAddress:
    """The address at the start of the root cell of a base64 BOC."""
    try:
        data = base64.b64decode(boc, validate=True)
    except ValueError as exc:
        raise BocError("not base64") from exc
    cells, roots = parse_boc(data)
    if len(roots) != 1:
        raise BocError("expected a single root cell")
    return read_address_bits(cells[roots[0]].bits)
