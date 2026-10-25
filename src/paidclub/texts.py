"""Member-facing and owner-facing message copy (Russian), as plain text plus buttons.

Messages are rendered when they are enqueued and stored in the outbox as final text, so a
template change never alters a message that is already waiting to be sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, tzinfo

from paidclub import callbacks
from paidclub.domain.subscriptions import ExpireReason
from paidclub.money import Money
from paidclub.notifications import Button, MessageDraft


def fmt_date(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m.%Y")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def when_phrase(offset: timedelta) -> str:
    """``timedelta(days=1)`` -> "Завтра", ``timedelta(days=3)`` -> "Через 3 дня"."""
    days = round(offset / timedelta(days=1))
    if days == 1:
        return "Завтра"
    if days >= 2:
        return f"Через {days} {plural(days, 'день', 'дня', 'дней')}"
    hours = max(round(offset / timedelta(hours=1)), 1)
    return f"Через {hours} {plural(hours, 'час', 'часа', 'часов')}"


def _join_buttons(invite_links: Sequence[tuple[str | None, str]]) -> tuple[Button, ...]:
    if len(invite_links) == 1:
        return (Button("Вступить в клуб", url=invite_links[0][1]),)
    return tuple(
        Button(f"Вступить: {title}" if title else "Вступить в клуб", url=link)
        for title, link in invite_links
    )


def _join_hint(invite_links: Sequence[tuple[str | None, str]]) -> str:
    if not invite_links:
        return "Ссылка для входа в клуб — в /start."
    return "Нажмите кнопку ниже и отправьте заявку на вступление — бот одобрит её сразу."


def activated(
    *,
    plan_title: str,
    period_end: datetime,
    invite_links: Sequence[tuple[str | None, str]],
    tz: tzinfo,
    reactivated: bool = False,
) -> MessageDraft:
    if reactivated:
        head = f"Оплата прошла, доступ к клубу снова открыт до {fmt_date(period_end, tz)}."
    else:
        head = f"Оплата прошла, подписка «{plan_title}» активна до {fmt_date(period_end, tz)}."
    return MessageDraft(f"{head}\n\n{_join_hint(invite_links)}", _join_buttons(invite_links))


def renewed_after_cancel(
    *,
    plan_title: str,
    period_end: datetime,
    invite_links: Sequence[tuple[str | None, str]],
    tz: tzinfo,
) -> MessageDraft:
    """A renewal charge sent before the member turned auto-renew off went through later."""
    head = (
        f"Списание за продление подписки «{plan_title}», начатое до отключения автопродления, "
        f"всё-таки прошло: доступ к клубу открыт до {fmt_date(period_end, tz)}. "
        "Автопродление остаётся выключенным, больше списаний не будет."
    )
    return MessageDraft(f"{head}\n\n{_join_hint(invite_links)}", _join_buttons(invite_links))


def renewed(*, plan_title: str, amount: Money, period_end: datetime, tz: tzinfo) -> MessageDraft:
    return MessageDraft(
        f"Подписка «{plan_title}» продлена до {fmt_date(period_end, tz)}. "
        f"Оплачено: {amount.human()}."
    )


def payment_failed(
    *,
    subscription_id: int,
    amount: Money,
    grace_until: datetime,
    next_retry_at: datetime | None,
    tz: tzinfo,
) -> MessageDraft:
    lines = [
        f"Не получилось списать {amount.human()} за продление подписки.",
        f"Доступ к клубу сохранится до {fmt_date(grace_until, tz)}.",
    ]
    if next_retry_at is not None:
        lines.append(
            f"Повторим попытку {fmt_date(next_retry_at, tz)}, а можно не ждать и оплатить сейчас "
            "(в том числе другой картой):"
        )
    else:
        lines.append("Автоматических попыток больше не будет. Чтобы остаться в клубе, оплатите:")
    return MessageDraft(
        "\n".join(lines),
        (Button(f"Оплатить {amount.human()}", callback_data=callbacks.pay(subscription_id)),),
    )


def stars_renewal_missing(
    *, subscription_id: int, amount: Money, grace_until: datetime, tz: tzinfo
) -> MessageDraft:
    return MessageDraft(
        f"Telegram не продлил подписку за {amount.human()} — возможно, на балансе не хватило "
        f"Stars.\nДоступ к клубу сохранится до {fmt_date(grace_until, tz)}. "
        "Чтобы остаться, оформите подписку заново:",
        (Button("Оплатить", callback_data=callbacks.pay(subscription_id)),),
    )


def reminder_auto_renew(
    *, subscription_id: int, amount: Money, period_end: datetime, offset: timedelta, tz: tzinfo
) -> MessageDraft:
    return MessageDraft(
        f"{when_phrase(offset)}, {fmt_date(period_end, tz)}, подписка продлится автоматически: "
        f"спишем {amount.human()} с сохранённой карты.",
        (Button("Отключить автопродление", callback_data=callbacks.cancel(subscription_id)),),
    )


def reminder_stars(
    *, subscription_id: int, amount: Money, period_end: datetime, offset: timedelta, tz: tzinfo
) -> MessageDraft:
    return MessageDraft(
        f"{when_phrase(offset)}, {fmt_date(period_end, tz)}, Telegram продлит подписку за "
        f"{amount.human()}. Проверьте, что на балансе хватает Stars.",
        (Button("Отключить автопродление", callback_data=callbacks.cancel(subscription_id)),),
    )


def reminder_manual(
    *, subscription_id: int, amount: Money, period_end: datetime, offset: timedelta, tz: tzinfo
) -> MessageDraft:
    return MessageDraft(
        f"{when_phrase(offset)}, {fmt_date(period_end, tz)}, заканчивается оплаченный период. "
        "Автосписание не настроено — оплатите продление, чтобы не потерять доступ:",
        (Button(f"Оплатить {amount.human()}", callback_data=callbacks.pay(subscription_id)),),
    )


def reminder_canceled(
    *, subscription_id: int, period_end: datetime, offset: timedelta, tz: tzinfo
) -> MessageDraft:
    return MessageDraft(
        f"{when_phrase(offset)}, {fmt_date(period_end, tz)}, закончится доступ к клубу: "
        "автопродление отключено.",
        (Button("Включить автопродление", callback_data=callbacks.resume(subscription_id)),),
    )


def access_ended(*, reason: ExpireReason) -> MessageDraft:
    if reason is ExpireReason.CANCELED:
        head = "Оплаченный период закончился, доступ к клубу закрыт."
    else:
        head = "Оплата за продление так и не прошла, поэтому доступ к клубу закрыт."
    return MessageDraft(
        f"{head} Вернуться можно в любой момент:",
        (Button("Выбрать тариф", callback_data=callbacks.PLANS),),
    )


def refunded(*, amount: Money, access_until: datetime | None, tz: tzinfo) -> MessageDraft:
    tail = (
        f"Доступ к клубу по оплаченному периоду сохранится до {fmt_date(access_until, tz)}."
        if access_until is not None
        else "Доступ к клубу закрыт."
    )
    return MessageDraft(
        f"Вам вернули {amount.human()} за подписку. Автопродление отключено, сохранённая карта "
        f"больше не используется. {tail} Вернуться можно в любой момент:",
        (Button("Выбрать тариф", callback_data=callbacks.PLANS),),
    )


def owner_alert(text: str) -> MessageDraft:
    return MessageDraft(f"Требует внимания. {text}")
