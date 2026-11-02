"""TON addresses and bags of cells, checked against values taken from toncenter's answers:
the address book it returns next to every result (raw form and the user-friendly one it
computed), and message bodies it decoded itself."""

from __future__ import annotations

import base64

import pytest

from paidclub.billing.tonaddress import (
    BocError,
    InvalidAddress,
    TonAddress,
    address_cell_boc,
    crc16,
    crc32c,
    parse_boc,
    read_address_bits,
    read_boc_address,
)

#: (raw, user-friendly) pairs from toncenter's address book.
ADDRESS_BOOK = [
    # the USDT master
    (
        "0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE",
        "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
    ),
    # a USDT jetton wallet
    (
        "0:32081D54752B9340A75C777A94AF0483F07329DD296EB4F915BB45E477B93F8A",
        "EQAyCB1UdSuTQKdcd3qUrwSD8HMp3SlutPkVu0Xkd7k_itc5",
    ),
    # a v4r2 wallet: toncenter shows wallets non-bounceable
    (
        "0:050AD67143942E003D5D65AC0E3F5427EE9BF7C55850384F0AC8318FD2E8A49F",
        "UQAFCtZxQ5QuAD1dZawOP1Qn7pv3xVhQOE8KyDGP0uiknz1V",
    ),
    (
        "0:0BFAF78B4E96E81675D72B5C2029A6C40B5B0104F270E5D48E751B9F92DF80DE",
        "UQAL-veLTpboFnXXK1wgKabEC1sBBPJw5dSOdRufkt-A3iek",
    ),
]

#: A forward payload exactly as toncenter returned it (with a CRC32-C), holding the text
#: comment it decoded as "100 Telegram Stars \n\nRef#mTJSJPtoq".
COMMENT_BOC = "te6cckEBAQEAKAAATAAAAAAxMDAgVGVsZWdyYW0gU3RhcnMgCgpSZWYjbVRKU0pQdG9xbTXCeg=="
#: A jetton internal_transfer body, which toncenter decoded as op 0x178d4519, query id
#: 15717841918299952104, amount 750000, from 0:AD0EF428...4D3AFF, the comment in a reference.
TRANSFER_BOC = (
    "te6cckEBAgEAfgABpxeNRRnaIP3ysANT6DC3GwgBWh3oUJtAvqGrgRJP1xLF0dUzLoFDxch5DIAumM6adf8AK0O9Ch"
    "NoF9Q1cCJJ+uJYujqmZdAoeLkPIZAF0xnTTr/EBwEASgAAAAA1MCBUZWxlZ3JhbSBTdGFycyAKClJlZiNsRTVsSDM4"
    "WUJCIK8y"
)


def _text(bits: str) -> str:
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8)).decode()


@pytest.mark.parametrize(("raw", "friendly"), ADDRESS_BOOK)
def test_raw_and_user_friendly_forms_are_the_same_address(raw: str, friendly: str) -> None:
    address = TonAddress.parse(friendly)
    assert address == TonAddress.parse(raw) == TonAddress.parse(raw.lower())
    assert address.raw == raw
    assert address.friendly(bounceable=friendly.startswith("EQ")) == friendly


def test_every_flavour_of_one_address_parses_to_it() -> None:
    master = TonAddress.parse(ADDRESS_BOOK[0][0])
    non_bounceable = master.friendly(bounceable=False)
    testnet = master.friendly(testnet=True)
    standard_base64 = base64.b64encode(base64.urlsafe_b64decode(ADDRESS_BOOK[0][1])).decode()
    assert non_bounceable.startswith("UQ")
    assert testnet.startswith("kQ")
    for text in (non_bounceable, testnet, standard_base64, f"  {ADDRESS_BOOK[0][1]}\n"):
        assert TonAddress.parse(text) == master
    assert TonAddress.parse("-1:" + "33" * 32).workchain == -1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not an address",
        "0:" + "ab" * 31,  # a byte short
        "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDt",  # checksum off by one
        "AACxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",  # unknown tag
        "999:" + "00" * 32,
    ],
)
def test_malformed_addresses_are_refused(text: str) -> None:
    with pytest.raises(InvalidAddress):
        TonAddress.parse(text)


def test_checksums_match_their_reference_values() -> None:
    assert crc16(b"123456789") == 0x31C3  # CRC-16/XMODEM check value
    assert crc32c(b"123456789") == 0xE3069283  # CRC-32C check value


def test_a_bag_of_cells_from_toncenter_is_read_and_its_checksum_verified() -> None:
    cells, roots = parse_boc(base64.b64decode(COMMENT_BOC))
    [root] = roots
    bits = cells[root].bits
    assert int(bits[:32], 2) == 0  # the text comment op
    assert _text(bits[32:]) == "100 Telegram Stars \n\nRef#mTJSJPtoq"

    damaged = bytearray(base64.b64decode(COMMENT_BOC))
    damaged[20] ^= 0x01
    with pytest.raises(BocError, match="checksum"):
        parse_boc(bytes(damaged))


def test_an_internal_transfer_reads_as_toncenter_decoded_it() -> None:
    cells, [root] = parse_boc(base64.b64decode(TRANSFER_BOC))
    bits = cells[root].bits
    assert int(bits[:32], 2) == 0x178D4519
    assert int(bits[32:96], 2) == 15717841918299952104
    length = int(bits[96:100], 2)
    assert int(bits[100 : 100 + 8 * length], 2) == 750000
    sender = read_address_bits(bits[100 + 8 * length :])
    assert sender.raw == "0:AD0EF4284DA05F50D5C08927EB8962E8EA999740A1E2E43C8640174C674D3AFF"
    [comment] = cells[root].refs
    assert _text(cells[comment].bits[32:]) == "50 Telegram Stars \n\nRef#lE5lH38YB"


def test_the_get_wallet_address_argument_is_a_one_cell_bag() -> None:
    owner = TonAddress.parse(ADDRESS_BOOK[2][1])
    boc = base64.b64decode(address_cell_boc(owner))
    # magic, no index or CRC, 1-byte sizes, 1 cell, 1 root, 0 absent, 36 bytes of cells, root 0,
    # then d1 = 0 (no refs), d2 = 67 (267 bits: 33 whole bytes and a partial one).
    assert boc[:13].hex() == "b5ee9c72" + "0101" + "010100" + "24" + "00" + "0043"
    assert len(boc) == 13 + 34
    assert boc[-1] & 0x1F == 0x10  # 3 more bits, then the completion tag: a 1 and zeros
    assert read_boc_address(address_cell_boc(owner)) == owner


@pytest.mark.parametrize(
    "boc",
    [
        "bm90IGEgYm9j",  # "not a boc"
        base64.b64encode(bytes.fromhex("b5ee9c72010101010024000043")).decode(),  # no cells
        COMMENT_BOC,  # a comment, not an address
        "***",
    ],
)
def test_anything_but_an_address_cell_is_refused(boc: str) -> None:
    with pytest.raises(BocError):
        read_boc_address(boc)
