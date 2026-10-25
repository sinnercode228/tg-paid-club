"""An aiogram session that answers Bot API calls from memory, plus builders for raw updates.

Nothing goes to the network.
Every call is recorded, so a test asserts on the requests the bot *made*::

    session = MockedSession()
    bot = Bot(TEST_TOKEN, session=session)
    session.add_error(BanChatMember, 400, "Bad Request: not enough rights to restrict member")
    ...
    [ban] = session.calls(BanChatMember)

Results and errors are turned into a real Bot API response body and parsed by aiogram's own
``check_response``, so handlers see the same objects and exceptions as in production
(``TelegramBadRequest``, ``TelegramRetryAfter`` with ``retry_after``, ...).
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any, cast, get_args

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    CreateChatInviteLink,
    EditChatInviteLink,
    GetChat,
    GetChatMember,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject, Update

TEST_TOKEN = "42:TEST-TOKEN"  # not a real token
BOT_ID = 42

#: What a scripted answer may be: a result (object, raw dict, bool), or a callable that gets
#: the method and returns one (or raises).
Answer = Any | Callable[[TelegramMethod[Any]], Any]


class _Error:
    def __init__(self, status: int, description: str, retry_after: int | None) -> None:
        self.status = status
        self.description = description
        self.retry_after = retry_after


class MockedSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._queued: defaultdict[type[TelegramMethod[Any]], deque[Answer | _Error]] = defaultdict(
            deque
        )
        self._defaults: dict[type[TelegramMethod[Any]], Answer] = {}
        self._message_ids = itertools.count(100)
        self.closed = False

    # --- scripting -------------------------------------------------------------------------

    def add_result(self, method: type[TelegramMethod[Any]], answer: Answer) -> None:
        """Answer the next call of ``method`` with ``answer`` (one-shot, in order)."""
        self._queued[method].append(answer)

    def add_error(
        self,
        method: type[TelegramMethod[Any]],
        status: int,
        description: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        """Fail the next call of ``method`` the way the Bot API would."""
        self._queued[method].append(_Error(status, description, retry_after))

    def set_result(self, method: type[TelegramMethod[Any]], answer: Answer) -> None:
        """Answer every call of ``method`` that has nothing queued with ``answer``."""
        self._defaults[method] = answer

    # --- inspection ------------------------------------------------------------------------

    def calls[M: TelegramMethod[Any]](self, method: type[M]) -> list[M]:
        return [request for request in self.requests if isinstance(request, method)]

    def api_methods(self) -> list[str]:
        """Names of the Bot API methods called, in order (``["getChatMember", ...]``)."""
        return [request.__api_method__ for request in self.requests]

    # --- BaseSession -----------------------------------------------------------------------

    # The signatures below are aiogram's BaseSession, ``timeout`` parameters included.
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> TelegramType:
        self.requests.append(method)
        queued = self._queued.get(type(method))
        answer: Answer | _Error
        if queued:
            answer = queued.popleft()
        elif type(method) in self._defaults:
            answer = self._defaults[type(method)]
        else:
            answer = self._default_answer(bot, method)
        if callable(answer):
            answer = answer(method)
        if isinstance(answer, _Error):
            body: dict[str, Any] = {
                "ok": False,
                "error_code": answer.status,
                "description": answer.description,
            }
            if answer.retry_after is not None:
                body["parameters"] = {"retry_after": answer.retry_after}
            status = answer.status
        else:
            body = {"ok": True, "result": _dump(answer)}
            status = 200
        response = self.check_response(bot, method, status, json.dumps(body))
        return cast(TelegramType, response.result)

    async def close(self) -> None:
        self.closed = True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:  # pragma: no cover - the bot never downloads files
        yield b""

    def _default_answer(self, bot: Bot, method: TelegramMethod[Any]) -> Any:
        """Plausible results for the calls the club bot makes; ``True`` for boolean methods."""
        now = int(datetime.now(UTC).timestamp())
        me = {"id": bot.id, "is_bot": True, "first_name": "Club", "username": "club_bot"}
        if isinstance(method, SendMessage):
            return {
                "message_id": next(self._message_ids),
                "date": now,
                "chat": {"id": method.chat_id, "type": "private"},
                "from": me,
                "text": method.text,
            }
        if isinstance(method, GetMe):
            return me
        if isinstance(method, GetChat):
            return {
                "id": method.chat_id,
                "type": "channel",
                "title": "Клуб",
                "accent_color_id": 0,
                "max_reaction_count": 11,
                "accepted_gift_types": {
                    "unlimited_gifts": False,
                    "limited_gifts": False,
                    "unique_gifts": False,
                    "premium_subscription": False,
                    "gifts_from_channels": False,
                },
            }
        if isinstance(method, GetChatMember):
            user = {"id": method.user_id, "is_bot": method.user_id == bot.id, "first_name": "U"}
            if method.user_id == bot.id:
                return chat_admin(user)
            return {"status": "member", "user": user}
        if isinstance(method, CreateChatInviteLink):
            return {
                "invite_link": f"https://t.me/+join{abs(int(method.chat_id))}",
                "creator": me,
                "creates_join_request": bool(method.creates_join_request),
                "is_primary": False,
                "is_revoked": False,
                "name": method.name,
            }
        if isinstance(method, EditChatInviteLink):
            return {
                "invite_link": method.invite_link,
                "creator": me,
                "creates_join_request": bool(method.creates_join_request),
                "is_primary": False,
                "is_revoked": False,
                "name": method.name,
            }
        returning = method.__returning__
        if returning is bool or bool in get_args(returning):
            return True  # editMessageText and friends may answer True instead of a Message
        raise AssertionError(f"MockedSession: no answer scripted for {method.__api_method__}")


def _dump(value: Any) -> Any:
    if isinstance(value, TelegramObject):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def chat_admin(user: dict[str, Any], **rights: bool) -> dict[str, Any]:
    """A ``ChatMemberAdministrator`` with every right, minus the ones set to ``False``."""
    names = (
        "can_be_edited",
        "is_anonymous",
        "can_manage_chat",
        "can_delete_messages",
        "can_manage_video_chats",
        "can_restrict_members",
        "can_promote_members",
        "can_change_info",
        "can_invite_users",
        "can_post_stories",
        "can_edit_stories",
        "can_delete_stories",
        "can_send_welcome_messages",
    )
    member: dict[str, Any] = {"status": "administrator", "user": user}
    for name in names:
        member[name] = rights.get(name, name not in ("can_be_edited", "is_anonymous"))
    return member


def buttons_of(method: TelegramMethod[Any]) -> list[InlineKeyboardButton]:
    """The inline buttons a ``sendMessage`` / ``editMessageText`` call carried, in order."""
    markup = getattr(method, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [button for row in markup.inline_keyboard for button in row]


async def feed(dispatcher: Dispatcher, bot: Bot, raw: dict[str, Any]) -> Any:
    """Process one raw update the way polling would."""
    return await dispatcher.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))


# --- raw updates, shaped like the JSON Telegram sends ------------------------------------------

_update_ids = itertools.count(1)


def user(
    user_id: int, *, username: str | None = None, first_name: str = "Участник"
) -> dict[str, Any]:
    data: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": first_name}
    if username is not None:
        data["username"] = username
    return data


def message_update(
    user_id: int,
    text: str,
    *,
    username: str | None = None,
    chat_id: int | None = None,
    chat_type: str = "private",
) -> dict[str, Any]:
    chat: dict[str, Any] = {"id": user_id if chat_id is None else chat_id, "type": chat_type}
    entities = []
    if text.startswith("/"):
        entities.append({"type": "bot_command", "offset": 0, "length": len(text.split()[0])})
    return {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_update_ids),
            "date": 1767225600,
            "chat": chat,
            "from": user(user_id, username=username),
            "text": text,
            "entities": entities,
        },
    }


def callback_update(user_id: int, data: str, *, message_id: int = 7) -> dict[str, Any]:
    return {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": f"cq{next(_update_ids)}",
            "from": user(user_id),
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 1767225600,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Club"},
                "text": "…",
            },
        },
    }


def join_request_update(
    chat_id: int,
    user_id: int,
    *,
    username: str | None = None,
    title: str = "Клуб",
    query_id: str | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "chat": {"id": chat_id, "type": "channel", "title": title},
        "from": user(user_id, username=username),
        "user_chat_id": user_id,
        "date": 1767225600,
    }
    if query_id is not None:
        request["query_id"] = query_id
    return {"update_id": next(_update_ids), "chat_join_request": request}


def my_chat_member_update(
    chat_id: int, new_member: dict[str, Any], *, by_user_id: int = 1, title: str = "Клуб"
) -> dict[str, Any]:
    """The bot's own membership in ``chat_id`` changed to ``new_member``."""
    bot_user = {"id": BOT_ID, "is_bot": True, "first_name": "Club"}
    return {
        "update_id": next(_update_ids),
        "my_chat_member": {
            "chat": {"id": chat_id, "type": "channel", "title": title},
            "from": user(by_user_id),
            "date": 1767225600,
            "old_chat_member": {"status": "left", "user": bot_user},
            "new_chat_member": {**new_member, "user": bot_user},
        },
    }


def chat_member_update(
    chat_id: int,
    user_id: int,
    *,
    old: str = "left",
    new: str = "member",
    new_member: dict[str, Any] | None = None,
    by_user_id: int | None = None,
    via_join_request: bool = False,
    invite_link: str | None = None,
    title: str = "Клуб",
) -> dict[str, Any]:
    """``user_id``'s membership in ``chat_id`` changed from status ``old`` to ``new`` (or to
    ``new_member``, a whole ``ChatMember`` such as :func:`chat_admin` builds)."""
    member = user(user_id)
    update: dict[str, Any] = {
        "chat": {"id": chat_id, "type": "supergroup", "title": title},
        "from": user(by_user_id if by_user_id is not None else user_id),
        "date": 1767225600,
        "old_chat_member": {"status": old, "user": member},
        "new_chat_member": {**(new_member or {"status": new}), "user": member},
    }
    if via_join_request:
        update["via_join_request"] = True
    if invite_link is not None:
        update["invite_link"] = {
            "invite_link": invite_link,
            "creator": {"id": 1, "is_bot": False, "first_name": "Admin"},
            "creates_join_request": False,
            "is_primary": True,
            "is_revoked": False,
        }
    return {"update_id": next(_update_ids), "chat_member": update}
