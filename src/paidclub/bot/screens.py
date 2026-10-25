"""What the member sees in the bot: plans, payment options, checkout, subscription status.

Screens are pure functions from data to a :class:`~paidclub.notifications.MessageDraft` (plain
text plus buttons, the same shape the outbox stores), so the copy is tested without Telegram.

Menu buttons carry :class:`Menu` callback data and edit the menu message in place. Buttons in
outbox messages (reminders, "payment failed") use the formats in :mod:`paidclub.callbacks`
and get their answer as a new message, so the reminder itself stays readable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum

from aiogram.filters.callback_data import CallbackData

from paidclub.club import CheckoutLink, SubscriptionView
from paidclub.config import PlanConfig
from paidclub.domain.subscriptions import SubStatus
from paidclub.money import Money
from paidclub.notifications import Button, MessageDraft
from paidclub.texts import fmt_date, plural


class Go(StrEnum):
    PLANS = "plans"
    PLAN = "plan"
    BUY = "buy"
    STATUS = "status"
    PAY = "pay"
    CANCEL = "cancel"
    RESUME = "resume"


class Menu(CallbackData, prefix="m"):
    """``m:<go>:<plan>:<provider>:<subscription id>``, e.g. ``m:buy:month:yookassa:0``."""

    go: Go
    plan: str = ""
    provider: str = ""
    sub: int = 0


PROVIDER_LABELS: dict[str, str] = {
    "fake": "Тестовая оплата",
    "yookassa": "Банковская карта",
    "stars": "Telegram Stars",
}

_PROVIDER_NOTES: dict[str, str] = {
    "fake": "демо: деньги не списываются, исход платежа выбирается на странице оплаты",
    "yookassa": "карта сохранится для автопродления",
    "stars": "продлевает сам Telegram раз в 30 дней",
}

DEFAULT_INTRO = (
    "Здравствуйте! Это бот закрытого клуба. После оплаты подписки бот пришлёт ссылку "
    "на вступление и сам одобрит заявку."
)

NO_SUBSCRIPTION_INTRO = (
    "У вас пока нет подписки. После оплаты бот пришлёт ссылку на вступление в клуб "
    "и сам одобрит заявку."
)

HELP = "Команды: /start — тарифы и оплата, /status — моя подписка."

#: After "resume" on a Telegram Stars subscription: the bot may only allow it, the member
#: turns renewal back on in Telegram.
STARS_RESUME_NOTE = (
    "Telegram снова разрешает продлить подписку, но включить продление нужно вам самим: "
    "в настройках Telegram откройте раздел Telegram Stars, найдите подписку на клуб и "
    "включите её. Как только Telegram её продлит, бот пришлёт подтверждение."
)
#: The resume button of a canceled Telegram Stars subscription.
STARS_RESUME_BUTTON = "Как снова включить продление"


@dataclass(frozen=True, slots=True)
class Offer:
    provider: str
    price: Money


@dataclass(frozen=True, slots=True)
class PlanOffers:
    """A plan and the ways it can be paid for right now (enabled providers with a price)."""

    plan: PlanConfig
    offers: tuple[Offer, ...]


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(name, name)


def period_text(unit: str, count: int) -> str:
    """``("month", 3)`` -> "3 месяца", ``("day", 30)`` -> "30 дней"."""
    if unit == "month":
        return f"{count} {plural(count, 'месяц', 'месяца', 'месяцев')}"
    return f"{count} {plural(count, 'день', 'дня', 'дней')}"


def every_text(unit: str, count: int) -> str:
    """``("month", 1)`` -> "каждый месяц", ``("month", 3)`` -> "каждые 3 месяца"."""
    if count == 1:
        return "каждый месяц" if unit == "month" else "каждый день"
    word = "каждый" if count % 10 == 1 and count % 100 != 11 else "каждые"
    return f"{word} {period_text(unit, count)}"


def _prices(offers: Sequence[Offer], sep: str) -> str:
    seen: list[str] = []
    for offer in offers:
        text = offer.price.human()
        if text not in seen:
            seen.append(text)
    return sep.join(seen)


def _join_buttons(invite_links: Sequence[tuple[str | None, str]]) -> list[Button]:
    if len(invite_links) == 1:
        return [Button("Вступить в клуб", url=invite_links[0][1])]
    return [
        Button(f"Вступить: {title}" if title else "Вступить в клуб", url=link)
        for title, link in invite_links
    ]


def _back_to_plans() -> Button:
    return Button("« Все тарифы", callback_data=Menu(go=Go.PLANS).pack())


def _to_status() -> Button:
    return Button("Моя подписка", callback_data=Menu(go=Go.STATUS).pack())


# --- plans and checkout ------------------------------------------------------------------------


def plans(catalog: Sequence[PlanOffers], *, intro: str = DEFAULT_INTRO) -> MessageDraft:
    available = [item for item in catalog if item.offers]
    if not available:
        return MessageDraft(f"{intro}\n\nОплата сейчас недоступна, загляните позже.")
    lines = [f"• {item.plan.title} — {_prices(item.offers, ' или ')}" for item in available]
    text = (
        f"{intro}\n\nТарифы:\n"
        + "\n".join(lines)
        + "\n\nПодписка продлевается автоматически, отключить автопродление можно "
        "в любой момент в /status."
    )
    buttons = tuple(
        Button(
            f"{item.plan.title} — {_prices(item.offers, ' / ')}",
            callback_data=Menu(go=Go.PLAN, plan=item.plan.code).pack(),
        )
        for item in available
    )
    return MessageDraft(text, buttons)


def plan_details(item: PlanOffers) -> MessageDraft:
    plan = item.plan
    period = period_text(plan.period_unit, plan.period_count)
    lines = [
        f"Тариф «{plan.title}»: {_prices(item.offers, ' или ')} за {period}.",
        f"Подписка продлевается {every_text(plan.period_unit, plan.period_count)}, "
        "пока вы не отключите автопродление.",
        "",
        "Способы оплаты:",
    ]
    for offer in item.offers:
        note = _PROVIDER_NOTES.get(offer.provider)
        label = f"{provider_label(offer.provider)} — {offer.price.human()}"
        lines.append(f"• {label}" + (f" ({note})" if note else ""))
    buttons = [
        Button(
            f"{provider_label(offer.provider)} — {offer.price.human()}",
            callback_data=Menu(go=Go.BUY, plan=plan.code, provider=offer.provider).pack(),
        )
        for offer in item.offers
    ]
    buttons.append(_back_to_plans())
    return MessageDraft("\n".join(lines), tuple(buttons))


def checkout(link: CheckoutLink, *, plan_title: str, renewal: bool = False) -> MessageDraft:
    """The pay button for a first payment, or for the next period (``renewal=True``)."""
    amount = link.amount.human()
    if renewal:
        head = f"К оплате: {amount} — продление подписки «{plan_title}»."
        tail = "Когда платёж пройдёт, подписка продлится, а бот пришлёт подтверждение."
    else:
        head = f"К оплате: {amount} — тариф «{plan_title}»."
        tail = "Когда платёж пройдёт, бот пришлёт сюда ссылку для вступления в клуб."
    lines = [head, f"Нажмите «Оплатить». {tail}"]
    if link.provider == "fake":
        lines.append("Это тестовая страница оплаты: на ней можно выбрать, пройдёт платёж или нет.")
    elif link.provider == "stars":
        lines.append("Оплата Telegram Stars откроется прямо в Telegram.")
    buttons = [Button(f"Оплатить {amount}", url=link.url)]
    buttons.append(_to_status() if renewal else _back_to_plans())
    return MessageDraft("\n\n".join(lines), tuple(buttons))


# --- status ------------------------------------------------------------------------------------


def status(
    view: SubscriptionView,
    *,
    plan_title: str,
    invite_links: Sequence[tuple[str | None, str]],
    tz: tzinfo,
    provider_renewed: bool,
    note: str | None = None,
) -> MessageDraft:
    """The member's subscription: what they have, until when, and what happens next."""

    def date(moment: datetime | None) -> str:
        assert moment is not None
        return fmt_date(moment, tz)

    price = view.price.human()
    lines: list[str] = []
    buttons: list[Button] = []
    menu_pay = Menu(go=Go.PAY, sub=view.id).pack()
    menu_cancel = Menu(go=Go.CANCEL, sub=view.id).pack()
    menu_resume = Menu(go=Go.RESUME, sub=view.id).pack()
    choose_plan = Button("Выбрать тариф", callback_data=Menu(go=Go.PLANS).pack())

    if view.status is SubStatus.ACTIVE:
        lines.append(f"Подписка «{plan_title}» активна до {date(view.period_end)}.")
        if provider_renewed:
            lines.append(f"Автопродление включено: Telegram сам продлит подписку за {price}.")
            buttons.append(Button("Отключить автопродление", callback_data=menu_cancel))
        elif view.has_saved_method:
            charge_at = view.next_charge_at or view.period_end
            lines.append(
                f"Автопродление включено: {date(charge_at)} спишем {price} с сохранённой карты."
            )
            buttons.append(Button("Отключить автопродление", callback_data=menu_cancel))
        else:
            lines.append(
                "Автосписание не настроено: чтобы не потерять доступ, оплатите продление "
                f"до {date(view.period_end)}."
            )
            buttons.append(Button(f"Оплатить продление {price}", callback_data=menu_pay))
            buttons.append(Button("Не продлевать", callback_data=menu_cancel))
    elif view.status is SubStatus.PAST_DUE:
        if not view.has_access:
            lines.append(f"Продление подписки «{plan_title}» не оплачено, доступ к клубу закрыт.")
        else:
            if provider_renewed:
                lines.append(
                    f"Telegram не продлил подписку «{plan_title}» — возможно, на балансе "
                    "не хватило Stars."
                )
            else:
                lines.append(f"Не получилось списать {price} за продление подписки «{plan_title}».")
            lines.append(f"Доступ к клубу сохранится до {date(view.grace_until)}.")
            if view.next_charge_at is not None and not provider_renewed:
                lines.append(f"Повторим попытку {date(view.next_charge_at)}.")
        buttons.append(Button(f"Оплатить {price}", callback_data=menu_pay))
    elif view.status is SubStatus.CANCELED:
        if view.has_access:
            lines.append(
                f"Автопродление отключено. Доступ к клубу по подписке «{plan_title}» "
                f"сохранится до {date(view.period_end)}."
            )
            label = STARS_RESUME_BUTTON if provider_renewed else "Включить автопродление"
            buttons.append(Button(label, callback_data=menu_resume))
        else:
            lines.append(
                f"Оплаченный период подписки «{plan_title}» закончился, доступ к клубу закрыт."
            )
            buttons.append(choose_plan)
    else:
        lines.append(
            f"Подписка «{plan_title}» закончилась, доступ к клубу закрыт. "
            "Вернуться можно в любой момент:"
        )
        buttons.append(choose_plan)

    if view.has_access and invite_links:
        lines.append(
            "\nЕсли вы ещё не в клубе, нажмите «Вступить в клуб» и отправьте заявку — "
            "бот одобрит её сразу."
        )
        buttons[:0] = _join_buttons(invite_links)
    if note:
        lines.insert(0, note)
    return MessageDraft("\n".join(lines), tuple(buttons))


#: ``/start`` payload of the button in the message to a declined join request.
JOIN_START_PAYLOAD = "join"


def join_declined(
    catalog: Sequence[PlanOffers], *, chat_title: str | None, bot_username: str | None = None
) -> MessageDraft:
    """Sent before declining a join request from someone without access.

    The bot may write to this person only now: ``user_chat_id`` works for five minutes and
    until the request is processed. Everything later (the join link after paying, reminders)
    needs them to have started the bot, so the button is a ``t.me/<bot>?start=`` link that
    does exactly that, rather than plan buttons they could pay through without ever writing
    to the bot.
    """
    where = f"в «{chat_title}»" if chat_title else "в клуб"
    if bot_username is None:
        intro = (
            f"Чтобы вступить {where}, нужна активная подписка. Оформите её здесь — после "
            "оплаты бот пришлёт ссылку, и заявку он одобрит сразу."
        )
        return plans(catalog, intro=intro)
    intro = (
        f"Чтобы вступить {where}, нужна активная подписка. Откройте бота кнопкой ниже и "
        "выберите тариф — после оплаты бот пришлёт ссылку, и заявку он одобрит сразу."
    )
    listed = plans(catalog, intro=intro)
    if not listed.buttons:
        return listed  # nothing to buy right now
    url = f"https://t.me/{bot_username}?start={JOIN_START_PAYLOAD}"
    return MessageDraft(listed.text, (Button("Открыть бота и выбрать тариф", url=url),))


def inline_urls(draft: MessageDraft) -> MessageDraft:
    """Move link buttons into the text. Telegram rejects some URLs as buttons (``localhost`` in
    a local demo), but the member can still open them from the message."""
    links = [f"{button.text}: {button.url}" for button in draft.buttons if button.url]
    kept = tuple(button for button in draft.buttons if button.url is None)
    return MessageDraft(draft.text + "\n\n" + "\n".join(links), kept)
