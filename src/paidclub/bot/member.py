"""The member-facing bot: plans, checkout, subscription status, auto-renew on and off.

Handlers stay thin: the operations live in :mod:`paidclub.club` (state changes, provider
calls) and the copy in :mod:`paidclub.bot.screens`. Each action returns a :class:`Reply`, which
the handler shows by editing the menu message (menu buttons) or as a new message (buttons in
outbox messages, commands).

Business errors (:class:`~paidclub.club.ClubError`) become a pop-up with their Russian
``user_message`` next to the current status, so the member always sees where they stand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import CallbackQuery, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import callbacks, club
from paidclub.bot import screens
from paidclub.bot.catalog import catalog, plan_offers
from paidclub.bot.screens import Go, Menu
from paidclub.bot.ui import show
from paidclub.container import Core
from paidclub.db import in_transaction
from paidclub.domain.subscriptions import LIVE_STATUSES, RenewalMode, SubStatus
from paidclub.models import Subscription
from paidclub.notifications import MessageDraft
from paidclub.repo import invite_links, upsert_member

log = logging.getLogger(__name__)

GENERIC_ERROR = "Что-то пошло не так. Попробуйте ещё раз через минуту."


@dataclass(frozen=True, slots=True)
class Reply:
    draft: MessageDraft
    #: Shown as a pop-up on the button the member pressed.
    alert: str | None = None


# --- actions -----------------------------------------------------------------------------------


def plans_reply(core: Core, *, intro: str = screens.DEFAULT_INTRO) -> Reply:
    return Reply(screens.plans(catalog(core), intro=intro))


async def home(core: Core, telegram_id: int) -> Reply:
    """``/start``: the subscription for members who have one, the plans for everyone else."""
    view = await club.status(core, telegram_id)
    if view is not None and view.status in LIVE_STATUSES:
        return await _status(core, view)
    return plans_reply(core)


async def status_reply(core: Core, telegram_id: int, *, note: str | None = None) -> Reply:
    view = await club.status(core, telegram_id)
    if view is None:
        return plans_reply(core, intro=screens.NO_SUBSCRIPTION_INTRO)
    return await _status(core, view, note=note)


async def _status(core: Core, view: club.SubscriptionView, *, note: str | None = None) -> Reply:
    async with core.sessions() as session:
        links = await invite_links(session)
    return Reply(
        screens.status(
            view,
            plan_title=_plan_title(core, view.plan_code),
            invite_links=links,
            tz=core.tz,
            provider_renewed=core.providers.renewal_mode(view.provider) is RenewalMode.PROVIDER,
            note=note,
        )
    )


def plan_reply(core: Core, plan_code: str) -> Reply:
    item = plan_offers(core, plan_code)
    if item is None or not item.offers:
        return Reply(plans_reply(core).draft, alert=club.PlanNotFound.user_message)
    return Reply(screens.plan_details(item))


async def buy_reply(core: Core, user: User, plan_code: str, provider: str) -> Reply:
    try:
        link = await club.start_checkout(
            core,
            telegram_id=user.id,
            plan_code=plan_code,
            provider=provider,
            username=user.username,
            first_name=user.first_name,
        )
    except club.AlreadySubscribed as exc:
        return await status_reply(core, user.id, note=exc.user_message)
    except club.ClubError as exc:
        return Reply(plans_reply(core).draft, alert=exc.user_message)
    return Reply(screens.checkout(link, plan_title=_plan_title(core, plan_code)))


async def pay_reply(core: Core, user: User, subscription_id: int) -> Reply:
    """Pay for the next period: after a failed renewal, or when no card is saved."""
    try:
        link = await club.start_repayment(
            core, telegram_id=user.id, subscription_id=subscription_id
        )
    except club.ClubError as exc:
        return await _refused(core, user.id, exc)
    async with core.sessions() as session:
        sub = await session.get(Subscription, subscription_id)
        plan_code = sub.plan_code if sub is not None else ""
    return Reply(screens.checkout(link, plan_title=_plan_title(core, plan_code), renewal=True))


async def toggle_reply(core: Core, user: User, subscription_id: int, *, enable: bool) -> Reply:
    operation = club.resume if enable else club.cancel
    try:
        view = await operation(core, telegram_id=user.id, subscription_id=subscription_id)
    except club.ClubError as exc:
        return await _refused(core, user.id, exc)
    note = None
    if enable:
        # A Stars subscription stays canceled until the member turns renewal on in Telegram.
        resumed = view.status is not SubStatus.CANCELED
        note = "Автопродление снова включено." if resumed else screens.STARS_RESUME_NOTE
    return await _status(core, view, note=note)


async def _refused(core: Core, telegram_id: int, exc: club.ClubError) -> Reply:
    log.info("member %s: %s", telegram_id, exc)
    current = await status_reply(core, telegram_id)
    return Reply(current.draft, alert=exc.user_message)


def _plan_title(core: Core, plan_code: str) -> str:
    plan = core.settings.plan(plan_code)
    return plan.title if plan is not None else plan_code


async def remember(core: Core, user: User) -> None:
    """Record the member; writing to the bot also clears a recorded "bot blocked"."""

    async def work(session: AsyncSession) -> None:
        await upsert_member(
            session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            now=core.clock.now(),
        )

    await in_transaction(core.sessions, work)


async def run_action(
    core: Core, user: User, go: Go, *, plan: str = "", provider: str = "", sub: int = 0
) -> Reply:
    if go is Go.PLANS:
        return plans_reply(core)
    if go is Go.STATUS:
        return await status_reply(core, user.id)
    if go is Go.PLAN:
        return plan_reply(core, plan)
    if go is Go.BUY:
        return await buy_reply(core, user, plan, provider)
    if go is Go.PAY:
        return await pay_reply(core, user, sub)
    return await toggle_reply(core, user, sub, enable=go is Go.RESUME)


# --- handlers ----------------------------------------------------------------------------------


async def on_start(message: Message, bot: Bot, core: Core) -> None:
    if message.from_user is None:
        return
    await remember(core, message.from_user)
    reply = await home(core, message.from_user.id)
    await show(bot, message.chat.id, reply.draft)


async def on_plans(message: Message, bot: Bot, core: Core) -> None:
    await show(bot, message.chat.id, plans_reply(core).draft)


async def on_status(message: Message, bot: Bot, core: Core) -> None:
    if message.from_user is None:
        return
    await remember(core, message.from_user)
    reply = await status_reply(core, message.from_user.id)
    await show(bot, message.chat.id, reply.draft)


async def on_other_text(message: Message, bot: Bot) -> None:
    await bot.send_message(chat_id=message.chat.id, text=screens.HELP, parse_mode=None)


async def on_menu(query: CallbackQuery, callback_data: Menu, bot: Bot, core: Core) -> None:
    await _answer(
        query,
        bot,
        core,
        callback_data.go,
        plan=callback_data.plan,
        provider=callback_data.provider,
        sub=callback_data.sub,
        edit=True,
    )


class OutboxButton(Filter):
    """Buttons in outbox messages (:mod:`paidclub.callbacks`): ``plans``, ``pay:<id>``, ..."""

    async def __call__(self, query: CallbackQuery) -> bool | dict[str, Any]:
        parsed = callbacks.parse(query.data or "")
        return {"button": parsed} if parsed is not None else False


_OUTBOX_ACTIONS: dict[str, Go] = {
    "plans": Go.PLANS,
    "pay": Go.PAY,
    "cancel": Go.CANCEL,
    "resume": Go.RESUME,
}


async def on_outbox_button(
    query: CallbackQuery, button: callbacks.Callback, bot: Bot, core: Core
) -> None:
    # Answered with a new message: the reminder or failure notice above stays as it was.
    go = _OUTBOX_ACTIONS[button.action]
    await _answer(query, bot, core, go, sub=button.subscription_id or 0, edit=False)


async def _answer(
    query: CallbackQuery,
    bot: Bot,
    core: Core,
    go: Go,
    *,
    plan: str = "",
    provider: str = "",
    sub: int = 0,
    edit: bool,
) -> None:
    user = query.from_user
    try:
        reply = await run_action(core, user, go, plan=plan, provider=provider, sub=sub)
    except Exception:
        log.exception("button %r from %s failed", query.data, user.id)
        await _answer_query(query, GENERIC_ERROR)
        return
    await _answer_query(query, reply.alert)
    target = query.message if edit and isinstance(query.message, Message) else None
    await show(bot, user.id, reply.draft, edit=target)


async def _answer_query(query: CallbackQuery, alert: str | None) -> None:
    """Stop the button's spinner; a query Telegram already timed out is not an error."""
    try:
        await query.answer(text=alert, show_alert=alert is not None)
    except TelegramBadRequest as exc:
        if "query is too old" not in exc.message.lower():
            raise
        log.info("callback query %s expired before it was answered", query.id)


def create_member_router() -> Router:
    router = Router(name="paidclub.member")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_start, CommandStart())
    router.message.register(on_plans, Command("plans"))
    router.message.register(on_status, Command("status"))
    router.callback_query.register(on_menu, Menu.filter())
    router.callback_query.register(on_outbox_button, OutboxButton())
    router.message.register(on_other_text, F.text)
    return router
