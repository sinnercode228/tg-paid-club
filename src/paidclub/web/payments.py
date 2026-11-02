"""Payment webhooks: ``POST /webhooks/{provider}``.

The route only moves bytes. The provider verifies the notification (an HMAC for the fake
provider; for YooKassa, a fresh ``GET`` of the payment with the shop credentials; for Crypto
Pay, both: the HMAC first, then ``getInvoices``) and turns it into events, and every event goes
through :func:`paidclub.payments.apply_event`, where duplicates are recognised. A webhook
delivered twice therefore answers 200 twice and pays the period once.

With ``PAIDCLUB_YOOKASSA_WEBHOOK_TOKEN`` set, YooKassa notifications are accepted only at
``/webhooks/yookassa/<token>``. Verifying a YooKassa notification costs a request to the shop
API, and the route is open to anyone, so without the token every forged notification costs
one; the token (or a proxy that lets only YooKassa's addresses through) keeps them out.

Status codes are chosen for the provider's retry logic (YooKassa retries anything but 200 for
24 hours):

* 200: processed, whatever the outcome (applied, duplicate, needs refund...);
* 400: not a genuine or well-formed notification, retrying will not help;
* 404: no such provider enabled, or not the secret path set for it; 413: body too large;
* 502: the provider's API could not confirm the payment right now, retry later;
* 503: lost a race with concurrent writers several times, retry later.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from paidclub.billing.base import PaymentRefunded, ProviderError, WebhookRejected
from paidclub.config import Settings
from paidclub.container import Core
from paidclub.db import ConcurrentUpdateError
from paidclub.payments import ApplyResult, apply_event, apply_refund

log = logging.getLogger(__name__)

#: Provider notifications are a few kilobytes; anything far larger is not one.
MAX_BODY_BYTES = 64 * 1024


class UnknownProvider(LookupError):
    pass


class BodyTooLarge(ValueError):
    pass


async def process_webhook(
    core: Core, provider_name: str, headers: Mapping[str, str], body: bytes
) -> list[ApplyResult]:
    """Verify a notification with its provider and apply every event in it.

    Raises :class:`UnknownProvider`, :class:`~paidclub.billing.base.WebhookRejected`,
    :class:`~paidclub.billing.base.ProviderError` or
    :class:`~paidclub.db.ConcurrentUpdateError`. The fake checkout page calls this too, so a
    demo payment takes exactly the path of a real one.
    """
    provider = core.providers.get(provider_name)
    if provider is None:
        raise UnknownProvider(provider_name)
    results = []
    for event in await provider.parse_webhook(headers, body):
        if isinstance(event, PaymentRefunded):
            results.append(await apply_refund(core, event))
        else:
            results.append(await apply_event(core, event))
    return results


async def read_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge(f"{declared} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge(f"more than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def path_token(settings: Settings, provider: str) -> str | None:
    """The secret path segment the provider's notifications must be posted to, if one is set."""
    if provider == "yookassa" and settings.yookassa_webhook_token is not None:
        return settings.yookassa_webhook_token.get_secret_value()
    return None


def _path_matches(settings: Settings, provider: str, token: str | None) -> bool:
    expected = path_token(settings, provider)
    if expected is None or token is None:
        return expected is None and token is None
    return hmac.compare_digest(token.encode(), expected.encode())


def _error(status: int, error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def create_webhook_router(core: Core) -> APIRouter:
    router = APIRouter(tags=["webhooks"])

    @router.post("/webhooks/{provider}")
    async def webhook(provider: str, request: Request) -> JSONResponse:
        return await handle(provider, None, request)

    @router.post("/webhooks/{provider}/{token}")
    async def webhook_at_secret_path(provider: str, token: str, request: Request) -> JSONResponse:
        return await handle(provider, token, request)

    async def handle(provider: str, token: str | None, request: Request) -> JSONResponse:
        if provider not in core.providers:
            return _error(404, "unknown provider")
        if not _path_matches(core.settings, provider, token):
            # Answered before reading the body: nothing is asked of the provider's API.
            return _error(404, "not found")
        try:
            body = await read_body(request)
        except BodyTooLarge:
            return _error(413, "body too large")
        try:
            results = await process_webhook(core, provider, request.headers, body)
        except UnknownProvider:
            return _error(404, "unknown provider")
        except WebhookRejected as exc:
            log.warning("rejected %s webhook: %s", provider, exc)
            return _error(400, "rejected")
        except ProviderError as exc:
            log.warning("%s webhook not confirmed, asking for a retry: %s", provider, exc)
            return _error(502, "provider unavailable, retry later")
        except ConcurrentUpdateError:
            log.warning("%s webhook lost repeated races, asking for a retry", provider)
            return _error(503, "busy, retry later")
        return JSONResponse(
            {
                "ok": True,
                "results": [
                    {"outcome": result.outcome.value, "subscription_id": result.subscription_id}
                    for result in results
                ],
            }
        )

    return router
