"""The USDT-on-TON payment page: ``GET /ton/pay/{comment}``.

Telegram buttons open only web links, so the bot sends the member here. The page shows the
owner's wallet, the amount still to send and the checkout's comment, each with a copy button,
and two links that open a wallet app with all three filled in: Tonkeeper's
``https://app.tonkeeper.com/transfer/...`` and the ``ton://transfer/...`` scheme TON wallets
register. The copy buttons are the main path; the links are a shortcut.

The page moves no money and records nothing: it reads the invoice, and the wallet poll
(:mod:`paidclub.ton_payments`) finds the transfer. While the invoice waits for money the page
reloads itself every :data:`REFRESH_SECONDS`. The copy script is its only script, allowed by
its hash in the Content-Security-Policy, as is the stylesheet.
"""

from __future__ import annotations

import base64
import hashlib
import html
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from paidclub.billing.ton import PAGE_PATH, TonProvider, format_units
from paidclub.container import Core
from paidclub.models import PaymentOutcome, TonInvoice, TonInvoiceStatus
from paidclub.ton_payments import find_invoice, ton_provider, usdt
from paidclub.web.pages import STYLE, page

REFRESH_SECONDS = 20

COPY_SCRIPT = """
for (const button of document.querySelectorAll("button[data-copy]")) {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = "Скопировано";
    } catch (error) {
      button.textContent = "Выделите текст выше и скопируйте";
    }
  });
}
""".strip()


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'sha256-{_sha256(STYLE)}'; "
        f"script-src 'sha256-{_sha256(COPY_SCRIPT)}'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex",
}


def _not_found() -> HTMLResponse:
    return page(
        "Счёт не найден",
        '<div class="card"><h1>Счёт не найден</h1>'
        '<p class="muted">Вернитесь в бот и откройте оплату заново.</p></div>',
        status_code=404,
        headers=HEADERS,
    )


def _field(label: str, value: str, shown: str | None = None) -> str:
    return (
        f'<div class="field"><div class="muted">{html.escape(label)}</div>'
        f"<code>{html.escape(shown or value)}</code>"
        f'<button type="button" data-copy="{html.escape(value)}">Скопировать</button></div>'
    )


def render_invoice(
    invoice: TonInvoice, provider: TonProvider, *, plan_title: str, until: str
) -> HTMLResponse:
    decimals = provider.decimals
    total = usdt(invoice.amount_units, decimals)
    got = usdt(invoice.received_units, decimals)
    parts = [
        '<div class="card">',
        f"<h1>{html.escape(plan_title)}</h1>",
        '<div class="muted">Оплата USDT в сети TON прямо на кошелёк клуба</div>',
        f'<div class="amount">{html.escape(total)}</div>',
    ]
    if invoice.status is TonInvoiceStatus.PAID:
        if invoice.outcome == PaymentOutcome.APPLIED.value:
            note = (
                "<strong>Оплата получена.</strong> Подписка активна, бот прислал "
                "подтверждение в Telegram."
            )
            css = "ok"
        else:
            note = (
                "<strong>Платёж получен, но не зачислен к подписке.</strong> Владелец клуба "
                "получил уведомление и свяжется с вами."
            )
            css = "warn"
        parts += [f'<div class="status {css}">{note}</div>', "</div>"]
        return page(f"Оплата: {plan_title}", "\n".join(parts), headers=HEADERS)

    left_units = invoice.amount_units - invoice.received_units
    if invoice.status is TonInvoiceStatus.EXPIRED and not invoice.received_units:
        parts += [
            '<div class="status bad"><strong>Время на оплату вышло.</strong> Откройте оплату '
            "в боте заново: бот выставит новый счёт.</div>",
            "</div>",
        ]
        return page(f"Оплата: {plan_title}", "\n".join(parts), headers=HEADERS)
    if invoice.received_units:
        parts.append(
            f'<div class="status warn"><strong>Пришло {html.escape(got)} из '
            f"{html.escape(total)}.</strong> Доплатите {html.escape(usdt(left_units, decimals))} "
            "с тем же комментарием.</div>"
        )
    tonkeeper, deeplink = provider.transfer_links(units=left_units, comment=invoice.comment)
    amount = format_units(left_units, decimals)
    parts += [
        _field("Кошелёк", provider.wallet_text),
        _field("Сумма, USDT", amount, f"{amount} USDT"),
        _field("Комментарий — обязательно", invoice.comment),
        f'<a class="button primary" href="{html.escape(tonkeeper)}">Открыть в Tonkeeper</a>',
        f'<a class="button" href="{html.escape(deeplink)}">Открыть в другом кошельке TON</a>',
        '<p class="muted">Отправляйте только USDT в сети TON и обязательно с этим '
        "комментарием: по нему бот найдёт перевод. Подтверждение придёт в Telegram в течение "
        f"минуты после перевода. Счёт действует до {html.escape(until)}.</p>",
        "</div>",
        f"<script>{COPY_SCRIPT}</script>",
    ]
    head = f'\n<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
    return page(f"Оплата: {plan_title}", "\n".join(parts), head=head, headers=HEADERS)


def create_ton_checkout_router(core: Core) -> APIRouter:
    router = APIRouter(tags=["ton checkout"])

    @router.get(PAGE_PATH + "/{comment}", response_class=HTMLResponse, include_in_schema=False)
    async def pay_page(comment: str) -> HTMLResponse:
        provider = ton_provider(core)
        if provider is None:
            return _not_found()
        invoice = await find_invoice(core, comment)
        if invoice is None:
            return _not_found()
        plan = core.settings.plan(invoice.subscription.plan_code)
        expires: datetime = invoice.expires_at.astimezone(core.tz)
        return render_invoice(
            invoice,
            provider,
            plan_title=plan.title if plan else invoice.subscription.plan_code,
            until=f"{expires:%d.%m.%Y %H:%M}",
        )

    return router
