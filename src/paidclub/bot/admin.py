"""Owner-only bot commands: ``/stats [days]`` and ``/export``.

The router answers only ``settings.owner_id`` and only in a private chat with the bot, so the
numbers never end up in a club group even when the owner types the command there. Updates
from anyone else are left unhandled and fall through to the member routers.

Include it before the member routers and put the commands in the owner's menu::

    create_dispatcher(
        core, extra_routers=[create_admin_router(core)], owner_commands=ADMIN_COMMANDS
    )

Do not set the owner's menu to ``ADMIN_COMMANDS`` alone: a chat-scoped command list replaces
the general one, so :func:`paidclub.bot.app.set_commands` repeats the member commands in it.
"""

from __future__ import annotations

from datetime import tzinfo
from decimal import Decimal

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import BotCommand, BufferedInputFile, Message

from paidclub.config import PlanConfig
from paidclub.container import Core
from paidclub.money import Money
from paidclub.stats import (
    MAX_WINDOW_DAYS,
    Report,
    club_report,
    export_payments,
    export_subscriptions,
    whole_units,
)
from paidclub.texts import fmt_date, plural

DEFAULT_DAYS = 30

ADMIN_COMMANDS = [
    BotCommand(command="stats", description="Статистика клуба: /stats или /stats 7"),
    BotCommand(command="export", description="Подписки и платежи в CSV"),
]

USAGE = (
    "Статистика за последние дни: /stats — за 30 дней, /stats 7 — за неделю, "
    f"можно до {MAX_WINDOW_DAYS}."
)


class OwnerOnly(BaseFilter):
    """Passes messages from the owner in a private chat with the bot."""

    def __init__(self, owner_id: int | None) -> None:
        self.owner_id = owner_id

    async def __call__(self, message: Message) -> bool:
        return (
            self.owner_id is not None
            and message.from_user is not None
            and message.from_user.id == self.owner_id
            and message.chat.type == ChatType.PRIVATE
        )


def parse_days(args: str | None) -> int | None:
    """``None``/empty -> 30; ``"7"`` -> 7; anything else, or out of range, -> ``None``."""
    if args is None or not args.strip():
        return DEFAULT_DAYS
    text = args.strip()
    if not text.isdigit():
        return None
    days = int(text)
    return days if 1 <= days <= MAX_WINDOW_DAYS else None


def _amounts(values: dict[str, Money], fallback: str = "RUB") -> str:
    """``"5 460 ₽ и 500 ⭐"``: rubles and stars side by side, never added up."""
    shown = [money.human() for money in values.values()]
    return " и ".join(shown) if shown else Money(0, fallback).human()


def _percent(rate: Decimal) -> str:
    value = (rate * 100).quantize(Decimal("0.1"))
    return f"{value:f}".replace(".", ",") + " %"


def _plan_currency(plans: list[PlanConfig], code: str) -> str:
    plan = next((plan for plan in plans if plan.code == code), None)
    return "XTR" if plan is not None and plan.price_rub is None else "RUB"


def format_stats(report: Report, *, plans: list[PlanConfig]) -> str:
    """The ``/stats`` reply, plain text."""
    tz: tzinfo = report.tz
    now = report.snapshot.total
    totals = report.totals
    days = len(report.days)
    generated = report.snapshot.at

    lines = [
        f"Клуб на {fmt_date(generated, tz)} {generated.astimezone(tz):%H:%M} ({tz})",
        "",
        f"Сейчас в клубе: {now.live}",
        f"— автопродление включено: {now.active}",
        f"— продление не прошло, идёт льготный период: {now.past_due}",
        f"— автопродление отключено, доступ до конца периода: {now.canceled}",
        f"MRR: {_amounts(whole_units(now.mrr))}",
        "",
        f"За {days} {plural(days, 'день', 'дня', 'дней')} "
        f"({fmt_date(report.start, tz)} — {fmt_date(generated, tz)})",
        f"Новые подписки: {totals.new}",
    ]
    if totals.reactivated:
        lines.append(f"Вернулись после окончания доступа: {totals.reactivated}")
    lines += [
        f"Продления: {totals.renewals}",
        f"Выручка: {_amounts(totals.revenue)} "
        f"({totals.payments} {plural(totals.payments, 'платёж', 'платежа', 'платежей')})",
        f"Неудачные списания: {totals.failed_charges}",
    ]
    if totals.renewals_not_charged:
        lines.append(
            f"Продления, которые нечем было списать (нет карты, способ оплаты выключен): "
            f"{totals.renewals_not_charged}"
        )
    lines += [
        f"Продление не прошло: {totals.dunning_started}, "
        f"оплатили в льготный период: {totals.recovered}",
        f"Отключили автопродление: {totals.cancellations}, включили обратно: {totals.resumed}",
        f"Доступ закончился: {totals.churned} "
        f"(не оплатили: {totals.churned_unpaid}, после отключения: "
        f"{totals.churned_after_cancel})",
    ]
    churn = report.churn_rate
    if churn is not None:
        lines.append(
            f"Отток: {_percent(churn)} — ушли {totals.churned} из {report.exposed}, "
            "у кого был доступ за период"
        )
    if totals.needs_attention:
        lines.append(
            f"Требуют внимания: {totals.needs_attention} "
            f"{plural(totals.needs_attention, 'платёж', 'платежа', 'платежей')} без зачисления "
            "(повтор или неверная сумма) — проверьте и верните деньги, список в /admin"
        )
    if report.plans:
        lines += ["", "По тарифам:"]
        for plan in report.plans:
            currency = _plan_currency(plans, plan.code)
            lines.append(
                f"{plan.title}: в клубе {plan.now.live}, выручка "
                f"{_amounts(plan.period.revenue, currency)}, MRR "
                f"{_amounts(whole_units(plan.now.mrr), currency)}"
            )
    lines += ["", "/stats 7 — за неделю, /export — выгрузка в CSV"]
    return "\n".join(lines)


def create_admin_router(core: Core) -> Router:
    router = Router(name="admin")
    router.message.filter(OwnerOnly(core.settings.owner_id))

    @router.message(Command("stats"))
    async def stats_command(message: Message, command: CommandObject) -> None:
        days = parse_days(command.args)
        if days is None:
            await message.answer(USAGE, parse_mode=None)
            return
        report = await club_report(core, days=days)
        await message.answer(format_stats(report, plans=core.settings.plans), parse_mode=None)

    @router.message(Command("export"))
    async def export_command(message: Message) -> None:
        async with core.sessions() as session:
            subscriptions = await export_subscriptions(session, core.tz)
            payments = await export_payments(session, core.tz)
        stamp = core.clock.now().astimezone(core.tz).strftime("%Y%m%d-%H%M")
        note = f"Время — {core.tz}, суммы — в рублях или Stars (колонка currency)."
        await message.answer_document(
            BufferedInputFile(subscriptions, filename=f"subscriptions-{stamp}.csv"),
            caption=f"Подписки. {note}",
            parse_mode=None,
        )
        await message.answer_document(
            BufferedInputFile(payments, filename=f"payments-{stamp}.csv"),
            caption=f"Платежи, включая неудачные. {note}",
            parse_mode=None,
        )

    return router
