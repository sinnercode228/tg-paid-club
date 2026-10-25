"""Callback data for inline buttons in outbox messages. The bot registers a handler per action.

Formats (all well under Telegram's 64-byte limit)::

    plans            show the plans and payment options
    pay:<sub_id>     pay for the next period of a subscription (after a failed renewal)
    cancel:<sub_id>  turn auto-renew off
    resume:<sub_id>  turn auto-renew back on
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["plans", "pay", "cancel", "resume"]
_WITH_ID: frozenset[str] = frozenset({"pay", "cancel", "resume"})

PLANS = "plans"


def pay(subscription_id: int) -> str:
    return f"pay:{subscription_id}"


def cancel(subscription_id: int) -> str:
    return f"cancel:{subscription_id}"


def resume(subscription_id: int) -> str:
    return f"resume:{subscription_id}"


@dataclass(frozen=True, slots=True)
class Callback:
    action: Action
    subscription_id: int | None = None


def parse(data: str) -> Callback | None:
    """Parse callback data; ``None`` for anything that is not ours."""
    if data == PLANS:
        return Callback("plans")
    action, _, raw_id = data.partition(":")
    if action not in _WITH_ID or not raw_id.isdigit():
        return None
    return Callback(action, int(raw_id))  # type: ignore[arg-type]
