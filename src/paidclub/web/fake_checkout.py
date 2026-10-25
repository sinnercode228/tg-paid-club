"""The fake provider's payment page: ``GET``/``POST /fake-checkout/{token}``.

It stands in for a card provider's hosted page in the demo. The member picks an outcome, the
page builds the signed notification the fake provider would send and hands it to
:func:`paidclub.web.payments.process_webhook`, the same verify-and-apply path a real webhook
takes. Nothing here changes a subscription directly.

The token in the URL is signed by the fake provider and carries the whole checkout (payment id,
subscription, amount), so the page needs no state of its own. Pressing a button twice, or
reloading after paying, is harmless: the payment id is the same, and a repeat is recognised as
a duplicate.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from paidclub.billing.base import WebhookRejected
from paidclub.billing.fake import CardKind, FakeCheckoutSession, FakeProvider
from paidclub.container import Core
from paidclub.db import ConcurrentUpdateError
from paidclub.models import Payment, PaymentOutcome, PaymentStatus
from paidclub.web.pages import page
from paidclub.web.payments import BodyTooLarge, process_webhook, read_body

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Choice:
    label: str
    hint: str
    succeeded: bool
    card: CardKind | None


CHOICES: dict[str, Choice] = {
    "card_ok": Choice(
        "Оплатить картой",
        "Карта сохранится для автопродления, продления будут проходить.",
        succeeded=True,
        card="ok",
    ),
    "card_decline": Choice(
        "Оплатить картой, которая откажет при продлении",
        "Первый платёж пройдёт, а списания при продлении будут отклонены: "
        "так видно повторные попытки и льготный период.",
        succeeded=True,
        card="decline",
    ),
    "no_card": Choice(
        "Оплатить без сохранения карты",
        "Период оплачен, но продлевать придётся вручную по кнопке в боте.",
        succeeded=True,
        card=None,
    ),
    "decline": Choice(
        "Банк отклоняет платёж",
        "Деньги не списываются, подписка не активируется.",
        succeeded=False,
        card=None,
    ),
}

DEMO_BANNER = "Демо-оплата: это тестовая страница, деньги не списываются."


def _page(title: str, content: str, *, status_code: int = 200) -> HTMLResponse:
    return page(title, content, banner=DEMO_BANNER, status_code=status_code)


def _not_found() -> HTMLResponse:
    return _page(
        "Ссылка недействительна",
        '<div class="card"><h1>Ссылка на оплату недействительна</h1>'
        '<p class="muted">Вернитесь в бот и откройте оплату заново.</p></div>',
        status_code=404,
    )


def _status_block(payment: Payment | None) -> tuple[str, bool]:
    """The result box for an already processed payment, and whether to offer the buttons."""
    if payment is None:
        return "", True
    if payment.status is PaymentStatus.FAILED:
        return (
            '<div class="status bad"><strong>Банк отклонил платёж.</strong> '
            "Можно попробовать ещё раз.</div>",
            True,
        )
    if payment.outcome is PaymentOutcome.APPLIED:
        return (
            '<div class="status ok"><strong>Оплата прошла.</strong> Подписка активна, '
            "бот пришлёт подтверждение в Telegram.</div>",
            False,
        )
    if payment.outcome is PaymentOutcome.NEEDS_REFUND:
        return (
            '<div class="status warn"><strong>Платёж получен, но этот период уже оплачен.</strong> '
            "Владелец клуба получил уведомление о возврате.</div>",
            False,
        )
    return (
        '<div class="status warn"><strong>Платёж получен, но не применён к подписке.</strong> '
        "Владелец клуба получил уведомление.</div>",
        False,
    )


def _render_checkout(checkout: FakeCheckoutSession, payment: Payment | None) -> HTMLResponse:
    status, offer = _status_block(payment)
    period = "первый" if checkout.for_period == 1 else f"№{checkout.for_period} (оплата продления)"
    parts = [
        '<div class="card">',
        f"<h1>{html.escape(checkout.plan_title)}</h1>",
        f'<div class="muted">{html.escape(checkout.description)}</div>',
        f'<div class="amount">{html.escape(checkout.amount.human())}</div>',
        "<dl>",
        f"<dt>Период</dt><dd>{html.escape(period)}</dd>",
        f"<dt>Платёж</dt><dd>{html.escape(checkout.payment_id)}</dd>",
        "</dl>",
        status,
    ]
    if offer:
        parts.append('<form method="post">')
        for index, (value, choice) in enumerate(CHOICES.items()):
            css = ' class="primary"' if index == 0 else ""
            parts.append(
                f'<button type="submit" name="choice" value="{value}"{css}>'
                f"{html.escape(choice.label)}<span>{html.escape(choice.hint)}</span></button>"
            )
        parts.append("</form>")
    elif checkout.return_url:
        parts.append(
            f'<a class="button primary" href="{html.escape(checkout.return_url)}">'
            "Вернуться в Telegram</a>"
        )
    else:
        parts.append('<p class="muted">Можно закрыть страницу и вернуться в Telegram.</p>')
    parts.append("</div>")
    return _page(f"Оплата: {checkout.plan_title}", "\n".join(parts))


def create_fake_checkout_router(core: Core) -> APIRouter:
    router = APIRouter(tags=["fake checkout"])

    def provider() -> FakeProvider | None:
        found = core.providers.get("fake")
        return found if isinstance(found, FakeProvider) else None

    def open_checkout(token: str) -> tuple[FakeProvider, FakeCheckoutSession] | None:
        fake = provider()
        if fake is None:
            return None
        try:
            return fake, fake.read_checkout(token)
        except (WebhookRejected, KeyError, ValueError):
            return None

    async def find_payment(payment_id: str) -> Payment | None:
        async with core.sessions() as session:
            return (
                await session.scalars(
                    select(Payment).where(
                        Payment.provider == "fake", Payment.provider_payment_id == payment_id
                    )
                )
            ).one_or_none()

    @router.get("/fake-checkout/{token}", response_class=HTMLResponse)
    async def checkout_page(token: str) -> HTMLResponse:
        opened = open_checkout(token)
        if opened is None:
            return _not_found()
        _, checkout = opened
        return _render_checkout(checkout, await find_payment(checkout.payment_id))

    @router.post("/fake-checkout/{token}")
    async def checkout_submit(token: str, request: Request) -> Response:
        opened = open_checkout(token)
        if opened is None:
            return _not_found()
        fake, checkout = opened
        try:
            form = parse_qs((await read_body(request, limit=4096)).decode("utf-8", "replace"))
        except BodyTooLarge:
            return _page(
                "Ошибка",
                '<div class="card"><h1>Слишком большой запрос</h1></div>',
                status_code=413,
            )
        choice = CHOICES.get((form.get("choice") or [""])[0])
        if choice is None:
            return _page(
                "Ошибка",
                '<div class="card"><h1>Выберите вариант оплаты</h1></div>',
                status_code=400,
            )
        headers, body = fake.checkout_webhook(
            checkout, succeeded=choice.succeeded, card=choice.card
        )
        try:
            await process_webhook(core, fake.name, headers, body)
        except ConcurrentUpdateError:
            log.warning("fake checkout %s lost repeated races", checkout.payment_id)
            return _page(
                "Попробуйте ещё раз",
                '<div class="card"><h1>Не получилось провести платёж</h1>'
                '<p class="muted">Сервер занят, нажмите кнопку ещё раз.</p></div>',
                status_code=503,
            )
        # Post/redirect/get: reloading the result page does not submit again.
        return RedirectResponse(request.url.path, status_code=303)

    return router
