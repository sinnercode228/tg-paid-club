"""Which plans can be bought right now, and with which providers."""

from __future__ import annotations

from paidclub.billing.base import BillingProvider
from paidclub.bot.screens import Offer, PlanOffers
from paidclub.container import Core


def enabled_providers(core: Core) -> list[BillingProvider]:
    """Registered providers, in the order the owner listed them in ``PAIDCLUB_PROVIDERS``."""
    order: dict[str, int] = {name: i for i, name in enumerate(core.settings.providers)}
    return sorted(core.providers, key=lambda provider: order.get(provider.name, len(order)))


def catalog(core: Core) -> list[PlanOffers]:
    """Every configured plan with the providers that have a price for it.

    A plan only priced in Stars has no offer while the Stars provider is off, and the quarter
    and year plans have no Stars offer at all (Telegram renews Stars subscriptions every 30
    days); such plans are simply not shown.
    """
    providers = enabled_providers(core)
    result = []
    for plan in core.settings.plans:
        offers = []
        for provider in providers:
            price = plan.price_for(provider.currency)
            if price is not None:
                offers.append(Offer(provider.name, price))
        result.append(PlanOffers(plan, tuple(offers)))
    return result


def plan_offers(core: Core, plan_code: str) -> PlanOffers | None:
    return next((item for item in catalog(core) if item.plan.code == plan_code), None)
