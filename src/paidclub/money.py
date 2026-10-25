"""Money as integer minor units plus an ISO currency code.

Amounts never touch floats. ``RUB`` has two decimal places (kopecks), ``XTR`` (Telegram Stars)
has none. Providers that want decimal strings (YooKassa: ``"990.00"``) use
:meth:`Money.to_decimal_str`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

#: Number of decimal places per supported currency.
CURRENCY_EXPONENTS: dict[str, int] = {"RUB": 2, "XTR": 0}

CURRENCY_SIGNS: dict[str, str] = {"RUB": "₽", "XTR": "⭐"}


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        if self.currency not in CURRENCY_EXPONENTS:
            raise ValueError(f"unsupported currency: {self.currency!r}")
        if self.minor < 0:
            raise ValueError("amounts are never negative here")

    @classmethod
    def from_decimal(cls, value: Decimal | str | int, currency: str) -> Money:
        """Parse ``"990.00"`` or ``Decimal("990")``; extra decimal places are an error."""
        if currency not in CURRENCY_EXPONENTS:
            raise ValueError(f"unsupported currency: {currency!r}")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"not a decimal amount: {value!r}") from exc
        exponent = CURRENCY_EXPONENTS[currency]
        scaled = amount.scaleb(exponent)
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{value!r} has more than {exponent} decimal places for {currency}")
        return cls(int(scaled), currency)

    def to_decimal(self) -> Decimal:
        exponent = CURRENCY_EXPONENTS[self.currency]
        quantum = Decimal(1).scaleb(-exponent)
        return Decimal(self.minor).scaleb(-exponent).quantize(quantum, rounding=ROUND_HALF_EVEN)

    def to_decimal_str(self) -> str:
        """``Money(99000, "RUB")`` -> ``"990.00"``; ``Money(500, "XTR")`` -> ``"500"``."""
        return f"{self.to_decimal():f}"

    def human(self) -> str:
        """Short display form for messages: ``"990 ₽"``, ``"1 490,50 ₽"``, ``"500 ⭐"``."""
        value = self.to_decimal()
        whole = int(value)
        fraction = value - whole
        text = f"{whole:,}".replace(",", " ")
        if fraction:
            text += f",{int(fraction.scaleb(CURRENCY_EXPONENTS[self.currency])):02d}"
        return f"{text} {CURRENCY_SIGNS[self.currency]}"
