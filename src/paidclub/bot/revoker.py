"""Removing a member from a club chat through the Bot API (the scheduler's ``AccessRevoker``).

The Bot API has no "kick": removal is ``banChatMember`` followed by
``unbanChatMember(only_if_banned=True)``. The member is out of the chat but not on its ban
list, so the same join-request link works again once they pay.

Two details keep a removal from turning into a permanent ban:

* The ban is given an ``until_date`` a few minutes ahead. If the unban call is lost (network
  error, crash between the two calls), Telegram lifts the ban by itself, and the scheduler's
  retry finds a short ban it recognises as its own and lifts it straight away.
* A member who is already banned *without* such a short expiry was banned by an admin; that
  ban is left alone.

Every final answer is returned as a :class:`~paidclub.access.RevokeOutcome`; transient
errors (network, 5xx, long flood waits) are raised so the scheduler retries later.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberOwner,
    ChatMemberRestricted,
)

from paidclub.access import RevokeOutcome
from paidclub.clock import Clock, SystemClock

log = logging.getLogger(__name__)

#: How long the removal ban lasts if the unban never arrives. Telegram treats bans shorter than
#: 30 seconds as permanent, so this stays well above that even with some clock skew.
BAN_SAFETY_WINDOW = timedelta(minutes=10)
#: A ban that ends this close to "now" is taken to be our own unfinished removal.
_OWN_BAN_SLACK = timedelta(minutes=5)

# Telegram error descriptions (lowercased substrings) that are final answers.
_NOT_PARTICIPANT = (
    "user not found",
    "member not found",
    "user_not_participant",
    "participant_id_invalid",
    "user_id_invalid",
    "invalid user_id",
)
_IS_ADMIN = ("user is an administrator", "can't remove chat owner", "user_admin_invalid")
_NO_RIGHTS = (
    "not enough rights",
    "chat_admin_required",
    "need administrator rights",
    "have no rights",
    "member list is inaccessible",
    "chat not found",
    "bot is not a member",
    "bot was kicked",
)


def classify(exc: TelegramAPIError) -> RevokeOutcome | None:
    """The final outcome a Bot API error stands for, or ``None`` if it is not a final one."""
    text = exc.message.lower()
    for markers, outcome in (
        (_IS_ADMIN, RevokeOutcome.IS_ADMIN),
        (_NOT_PARTICIPANT, RevokeOutcome.NOT_PARTICIPANT),
        (_NO_RIGHTS, RevokeOutcome.NO_RIGHTS),
    ):
        if any(marker in text for marker in markers):
            return outcome
    if isinstance(exc, TelegramForbiddenError):
        # The bot was removed from the chat or lost admin status.
        return RevokeOutcome.NO_RIGHTS
    return None


class TelegramAccessRevoker:
    def __init__(
        self,
        bot: Bot,
        *,
        clock: Clock | None = None,
        unban_attempts: int = 3,
        retry_delay: float = 1.0,
        max_flood_wait: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        # Telegram compares until_date with real time, so this must be the real clock in
        # production; tests pass a fake one.
        self._clock = clock or SystemClock()
        self._unban_attempts = max(unban_attempts, 1)
        self._retry_delay = retry_delay
        self._max_flood_wait = max_flood_wait
        self._sleep = sleep

    async def revoke(self, chat_id: int, user_id: int) -> RevokeOutcome:
        try:
            member = await self._bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            return self._final(exc, chat_id, user_id)

        if isinstance(member, ChatMemberOwner | ChatMemberAdministrator):
            return RevokeOutcome.IS_ADMIN
        if isinstance(member, ChatMemberLeft):
            return RevokeOutcome.NOT_PARTICIPANT
        if isinstance(member, ChatMemberRestricted) and not member.is_member:
            return RevokeOutcome.NOT_PARTICIPANT
        if isinstance(member, ChatMemberBanned):
            if not self._is_own_ban(member):
                log.info("user %s is banned in %s by an admin; leaving the ban", user_id, chat_id)
                return RevokeOutcome.NOT_PARTICIPANT
            await self._unban(chat_id, user_id)
            return RevokeOutcome.REMOVED

        try:
            await self._bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                until_date=self._clock.now() + BAN_SAFETY_WINDOW,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            return self._final(exc, chat_id, user_id)
        await self._unban(chat_id, user_id)
        return RevokeOutcome.REMOVED

    def _is_own_ban(self, member: ChatMemberBanned) -> bool:
        until = member.until_date
        if until.timestamp() <= 0:  # 0 means "banned forever"
            return False
        return until <= self._clock.now() + BAN_SAFETY_WINDOW + _OWN_BAN_SLACK

    def _final(self, exc: TelegramAPIError, chat_id: int, user_id: int) -> RevokeOutcome:
        outcome = classify(exc)
        if outcome is None:
            raise exc
        log.info("removing %s from %s: %s (%s)", user_id, chat_id, outcome.value, exc.message)
        return outcome

    async def _unban(self, chat_id: int, user_id: int) -> None:
        """Lift the removal ban, riding out short network trouble and flood waits.

        If it still fails, the error propagates: the scheduler retries the removal later, and
        the ban expires on its own after :data:`BAN_SAFETY_WINDOW` in any case.
        """
        for attempt in range(1, self._unban_attempts + 1):
            try:
                await self._bot.unban_chat_member(
                    chat_id=chat_id, user_id=user_id, only_if_banned=True
                )
                return
            except TelegramRetryAfter as exc:
                if attempt == self._unban_attempts or exc.retry_after > self._max_flood_wait:
                    raise
                await self._sleep(exc.retry_after)
            except (TelegramNetworkError, TelegramServerError):
                if attempt == self._unban_attempts:
                    raise
                await self._sleep(self._retry_delay * attempt)
