"""Payment webhooks: ``POST /webhooks/{provider}``.

The route only moves bytes. The provider verifies the notification (an HMAC for the fake
provider; for YooKassa, a fresh ``GET`` of the payment with the shop credentials) and turns it
into events, and every event goes through :func:`paidclub.payments.apply_event`, where
duplicates are recognised. A webhook delivered twice therefore answers 200 twice and pays the
period once.

Status codes are chosen for the provider's retry logic (YooKassa retries anything but 200 for
24 hours):

* 200: processed, whatever the outcome (applied, duplicate, needs refund...);
* 400: not a genuine or well-formed notification, retrying will not help;
* 404: no such provider enabled; 413: body too large;
* 502: the provider's API could not confirm the payment right now, retry later;
* 503: lost a race with concurrent writers several times, retry later.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from paidclub.billing.base import ProviderError, WebhookRejected
from paidclub.container import Core
from paidclub.db import ConcurrentUpdateError
from paidclub.payments import ApplyResult, apply_event

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
    events = await provider.parse_webhook(headers, body)
    return [await apply_event(core, event) for event in events]


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


def _error(status: int, error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def create_webhook_router(core: Core) -> APIRouter:
    router = APIRouter(tags=["webhooks"])

    @router.post("/webhooks/{provider}")
    async def webhook(provider: str, request: Request) -> JSONResponse:
        if provider not in core.providers:
            return _error(404, "unknown provider")
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
