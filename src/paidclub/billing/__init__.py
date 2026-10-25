"""Billing providers and the registry the rest of the app looks them up in."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from paidclub.billing.base import BillingProvider
from paidclub.domain.subscriptions import RenewalMode

#: Providers that renew on their own schedule. Known by name so a process that has not
#: registered the provider (a scheduler without a bot) still treats its subscriptions right.
PROVIDER_RENEWED = frozenset({"stars"})


class ProviderNotConfigured(LookupError):
    pass


class ProviderRegistry:
    def __init__(self, providers: Iterable[BillingProvider] = ()) -> None:
        self._providers: dict[str, BillingProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: BillingProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider

    def get(self, name: str) -> BillingProvider | None:
        return self._providers.get(name)

    def require(self, name: str) -> BillingProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderNotConfigured(f"billing provider {name!r} is not enabled")
        return provider

    def renewal_mode(self, name: str) -> RenewalMode:
        provider = self._providers.get(name)
        if provider is not None:
            merchant = provider.supports_merchant_renewal
        else:
            merchant = name not in PROVIDER_RENEWED
        return RenewalMode.MERCHANT if merchant else RenewalMode.PROVIDER

    def names(self) -> list[str]:
        return list(self._providers)

    def __iter__(self) -> Iterator[BillingProvider]:
        return iter(self._providers.values())

    def __contains__(self, name: object) -> bool:
        return name in self._providers
