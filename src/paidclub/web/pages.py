"""Pages for members in the browser: the shared look, and ``GET /paid``.

``/paid`` is where YooKassa sends the member back after the payment page unless
``PAIDCLUB_CHECKOUT_RETURN_URL`` says otherwise. YooKassa sends them there whether or not they
paid, and the notification may still be on its way, so the page promises nothing: it tells
them to go back to the bot, which confirms the payment and sends the join link.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from paidclub.config import RETURN_PAGE_PATH

log = logging.getLogger(__name__)

#: Returns the bot's username for a t.me link, or None when there is no bot.
BotUsername = Callable[[], Awaitable[str | None]]

#: How long the page waits for the bot's username (cached after the first getMe).
USERNAME_TIMEOUT_SECONDS = 3.0

STYLE = """
:root {
  --bg: #f4f5f7; --card: #ffffff; --text: #1d2129; --muted: #646a73; --line: #e3e5e8;
  --accent: #2b6de8; --accent-text: #ffffff; --ok: #1f8a4c; --bad: #c4372f; --warn: #a86400;
  --banner: #fff4d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15171a; --card: #1f2226; --text: #e8eaed; --muted: #9aa0a8; --line: #33373d;
    --accent: #5b8ff0; --accent-text: #0d1117; --ok: #4cc27f; --bad: #ef6b62; --warn: #e0a43a;
    --banner: #3a3220;
  }
}
:root[data-theme="dark"] {
  --bg: #15171a; --card: #1f2226; --text: #e8eaed; --muted: #9aa0a8; --line: #33373d;
  --accent: #5b8ff0; --accent-text: #0d1117; --ok: #4cc27f; --bad: #ef6b62; --warn: #e0a43a;
  --banner: #3a3220;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px; background: var(--bg); color: var(--text);
  font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 460px; margin: 0 auto; }
.banner {
  background: var(--banner); border-radius: 10px; padding: 10px 14px; font-size: 14px;
  margin-bottom: 16px;
}
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px;
}
h1 { font-size: 20px; margin: 0 0 4px; }
.amount { font-size: 32px; font-weight: 700; margin: 12px 0 4px; }
.muted { color: var(--muted); font-size: 14px; }
dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 12px; margin: 16px 0 0; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; }
form { margin: 0; }
button, .button {
  display: block; width: 100%; margin-top: 12px; padding: 12px 14px; border-radius: 10px;
  border: 1px solid var(--line); background: transparent; color: var(--text);
  font: inherit; text-align: left; cursor: pointer; text-decoration: none;
}
button.primary, .button.primary {
  background: var(--accent); border-color: var(--accent); color: var(--accent-text);
}
button span { display: block; font-size: 13px; opacity: 0.8; margin-top: 2px; }
.status {
  margin-top: 16px; padding: 12px 14px; border-radius: 10px; border: 1px solid var(--line);
}
.status.ok { border-color: var(--ok); } .status.ok strong { color: var(--ok); }
.status.bad { border-color: var(--bad); } .status.bad strong { color: var(--bad); }
.status.warn { border-color: var(--warn); } .status.warn strong { color: var(--warn); }
"""


def page(
    title: str, content: str, *, banner: str | None = None, status_code: int = 200
) -> HTMLResponse:
    top = f'<div class="banner">{html.escape(banner)}</div>\n' if banner else ""
    document = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body><main>
{top}{content}
</main></body>
</html>"""
    return HTMLResponse(document, status_code=status_code)


def render_return_page(bot_username: str | None) -> HTMLResponse:
    if bot_username:
        action = (
            f'<a class="button primary" href="https://t.me/{html.escape(bot_username)}">'
            "Открыть бота</a>"
        )
    else:
        action = '<p class="muted">Можно закрыть страницу и вернуться в Telegram.</p>'
    return page(
        "Вернитесь в Telegram",
        '<div class="card"><h1>Вернитесь в Telegram</h1>'
        "<p>Если оплата прошла, бот пришлёт подтверждение и ссылку для вступления в клуб, "
        "как только платёж подтвердится.</p>"
        '<p class="muted">Если платёж не прошёл или страница оплаты была закрыта, '
        "оплатить можно снова из бота.</p>"
        f"{action}</div>",
    )


def create_pages_router(bot_username: BotUsername | None = None) -> APIRouter:
    router = APIRouter(tags=["pages"])

    @router.get(RETURN_PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
    async def return_page() -> HTMLResponse:
        username = None
        if bot_username is not None:
            try:
                async with asyncio.timeout(USERNAME_TIMEOUT_SECONDS):
                    username = await bot_username()
            except Exception as exc:  # the page works without the link
                log.warning("return page without the bot link: %s", type(exc).__name__)
        return render_return_page(username)

    return router
