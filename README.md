# tg-paid-club

English · [Русский](README.ru.md)

[![CI](https://github.com/sinnercode228/tg-paid-club/actions/workflows/ci.yml/badge.svg)](https://github.com/sinnercode228/tg-paid-club/actions/workflows/ci.yml)

A Telegram bot that sells access to a private channel or group by subscription. Members pay by
card through YooKassa or with Telegram Stars. The bot lets them in through join requests,
charges renewals with the saved card (YooKassa) or applies the renewals Telegram makes (Stars),
reminds members before a renewal or the end of access, retries failed charges during a grace
period and removes members whose access has ended. The owner gets `/stats` and `/export` in the
bot and a dashboard at `/admin`. It is one Python process: aiogram 3, FastAPI and SQLAlchemy 2
async on SQLite or PostgreSQL.

This is a demo project, not a client's bot, and no club runs on it. The members, payments and
revenue in the screenshot below come from `paidclub simulate`: 300 made-up members over 120
days of simulated time, paying through the built-in fake provider, which moves no money. The
messages members see are in Russian, because the clubs this is meant for are Russian-speaking.
The code and this README are in English, and there is a [Russian README](README.ru.md).

![The /admin dashboard on a database filled by paidclub simulate: simulated members and revenue, not a real club](docs/screenshots/dashboard.png)

*Simulated data: `make demo` on 25 October 2026. Nobody paid these amounts.*

## What goes wrong without it

**The invite link leaks.** An ordinary `t.me/+...` link lets in whoever has it. A member
forwards it to a friend or posts it in a chat, and people join without paying. Here every
club chat has one link created with `creates_join_request=True`, so following it only sends a
join request. The bot approves the request when the person's subscription gives access right
now, and declines it otherwise. People who get in another way, through an older link or added
by a member, are removed as soon as they join. A public username and the "members can add
members" permission are reported to the owner, because both let people skip the request.

**A repeated webhook extends the period twice.** YooKassa sends a notification again until it
gets a 200, for up to 24 hours. A slow answer or a proxy timeout is enough for a second
delivery. A handler that adds a month on every delivery gives two months for one payment.
Here `(provider, provider_payment_id)` is unique in `payments`, so the second delivery is a
duplicate and changes nothing. A different payment for a period that is already paid is
recorded, not applied, and the owner is asked to refund it. A partial unique index on
`(subscription_id, for_period)` guarantees that no period is paid twice, whichever code path
tries.

**Renewal dates drift.** `now + 30 days` on every payment moves every later date by however
late the charge ran, and a month is not 30 days. Here periods are counted from an anchor: the
n-th period ends at `anchor + n × plan period`. A renewal extends from the end of the current
period, never from "now". Month ends are clamped and come back: a subscription started on 31
January renews on 28 (or 29) February, then on 31 March.

**A failed charge either keeps the member forever or throws them out at once.** If nothing
reacts to a declined card, the member stays in for free. If the bot removes them the minute
the bank says no, it loses people whose card had a bad day. Here the subscription goes
`past_due`: access stays for the grace period (3 days by default), the charge is retried 1
and 3 days after the first failure, and the member gets a message with a "pay" button. When
grace runs out with nothing paid, the subscription expires and the member is removed.

**A removed member is banned for good.** The Bot API has no "kick". `banChatMember` alone
puts the user on the chat's ban list, and the join link does not work for them after they pay
again. Here removal is `banChatMember` with an `until_date` ten minutes ahead, followed by
`unbanChatMember(only_if_banned=True)`. If the unban call is lost, the short ban runs out on
its own. The owner and the admins listed in the settings are never touched. "User not a
participant" and missing rights are recorded as answers rather than retried forever.

**A reminder is sent twice.** A second worker, a restart or a retried job sends "your
subscription renews tomorrow" again. Here every message is a row in an outbox table with a
unique key, such as `reminder:<subscription>:<period end>:<offset>`, written in the same
transaction as the change that caused it. Sending is claimed with a lease, Telegram's 429
`retry_after` pauses the whole outbox, and a 403 (the member blocked the bot) closes the
message as undeliverable instead of retrying it.

**The webhook body is trusted.** YooKassa does not sign its notifications. Its documentation
suggests checking the payment's current status or the sender's IP address. A handler that
reads `"status": "succeeded"` from the POST body activates anyone who can send JSON to the
URL. Here the body is used for the payment id only. The app then calls
`GET /v3/payments/{id}` with the shop's credentials and applies the status from that answer.

**A crash in the middle of a charge charges twice.** A worker sends the renewal charge and
dies before it records the answer, and the next run sends it again. Here the attempt row is
committed before the call, and the call carries an `Idempotence-Key` built from the
subscription, the period and the attempt number (`paidclub-42-2-0`). A repeated request with
the same key gets YooKassa's first answer back. YooKassa remembers a key for 24 hours, so after
23 hours the app looks the payment up by its metadata instead of sending the request again.

## How a membership goes, end to end

With YooKassa, from setting up the chat to removal. Where Telegram Stars or the fake provider
differ, it says so.

1. **The chat.** The owner adds the bot to a private channel or supergroup as an administrator
   who can invite users, and, in a supergroup, ban users. On startup, and whenever its own
   status in a club chat changes (`my_chat_member`), the bot checks its rights with `getChat`
   and `getChatMember` and creates one link with
   `createChatInviteLink(creates_join_request=True, name="Подписчики клуба (по заявке)")`. The
   link is stored in `club_chats` and reused after restarts. Each check confirms it with
   `editChatInviteLink`, and a link an admin revoked is replaced. Problems go to the owner in
   Telegram, once a day per chat. Code: [`bot/chats.py`](src/paidclub/bot/chats.py).
2. **Choosing a plan.** `/start` shows the plans. Picking a plan and a payment method creates a
   `pending` subscription, with the plan's price and period copied onto it, so a later price
   change affects new subscribers only. Then the provider creates the payment:
   - YooKassa: `POST /v3/payments` with `amount {"value": "990.00", "currency": "RUB"}`,
     `capture: true`, `save_payment_method: true`, a `redirect` confirmation and our
     `metadata` (subscription, member, period, purpose), under a fresh `Idempotence-Key`.
     The member gets `confirmation_url` as a button. The payment id goes into `checkouts`.
   - Stars: `createInvoiceLink(currency="XTR", prices=[one price],
     subscription_period=2592000, payload="paidclub:<subscription>:<period>")`.
   - Fake: a signed link to `/fake-checkout/<token>` on this app, a page where you choose
     whether the payment goes through and whether the card is saved for renewals.

   Code: [`club.py`](src/paidclub/club.py), [`billing/`](src/paidclub/billing).
3. **The payment arrives.** YooKassa posts `payment.succeeded` to `/webhooks/yookassa` (or to
   `/webhooks/yookassa/<token>`, see `PAIDCLUB_YOOKASSA_WEBHOOK_TOKEN`). The app takes
   `object.id` from the body, fetches the payment with `GET /v3/payments/{id}` and applies
   what that returns. If `payment_method.saved` is `true`, the method id is kept for renewals.
   If the notification never comes, the scheduler looks the payment up anyway: 5 minutes after
   the checkout, every 15 minutes for two hours, then hourly, for up to 48 hours. In that case
   the owner is told to check the notification URL. Code:
   [`billing/yookassa.py`](src/paidclub/billing/yookassa.py),
   [`checkouts.py`](src/paidclub/checkouts.py).
   - Stars: Telegram first sends `pre_checkout_query`. The bot answers it within 5 seconds
     (Telegram allows 10) after checking the payload, the payer, the amount and that the
     period is not paid yet. Then comes `successful_payment`. It is stored in
     `stars_payment_updates` before it is applied, because Telegram delivers it only once.
     `subscription_expiration_date` sets the period end, and the first
     `telegram_payment_charge_id` is kept for cancelling later.
4. **Activation.** One transaction records the payment, moves the subscription from `pending`
   to `active` with the first period starting now, and puts "Оплата прошла" with the "Вступить
   в клуб" button into the outbox. The outbox sends it with `sendMessage`. Code:
   [`payments.py`](src/paidclub/payments.py).
5. **Joining.** The link sends a `chat_join_request`. A member with access is approved with
   `approveChatJoinRequest`. Anyone else first gets a message sent to `user_chat_id`, which
   the Bot API allows for five minutes and only until the request is answered. It shows the
   plans and has a `t.me/<bot>?start=join` button. Then the request is declined with
   `declineChatJoinRequest`. Every decision goes into `access_events`. Flood control,
   network errors and 5xx answers are retried, because Telegram does not deliver a join
   request again. A bot that a chat has assigned to process its join requests gets them as
   queries and answers with `answerChatJoinRequestQuery` instead. Code:
   [`bot/join.py`](src/paidclub/bot/join.py).
6. **Reminders.** 3 days and 1 day before the period ends. The text depends on the case: auto
   renewal with a saved card (with a button to turn it off), no saved card (with a pay
   button), a Stars subscription (check your Stars balance), or auto-renew already off (with a
   button to turn it back on). A worker that was down during the 3-day window sends only the
   1-day reminder.
7. **Renewal.** At the period end the scheduler claims the subscription. It commits a
   `charge_attempts` row and then sends `POST /v3/payments` with `payment_method_id`,
   `capture: true` and `Idempotence-Key: paidclub-<subscription>-<period>-<attempt>`. A
   success extends the subscription from the old period end, and the member gets "Подписка
   продлена до …". A `pending` answer is checked with `GET /v3/payments/{id}` every 15
   minutes, and the same request is not sent again.
   - Stars: Telegram charges the renewal itself and sends `successful_payment` with
     `is_recurring`. If none has arrived 60 minutes after the period end, the subscription
     goes `past_due` without retries, since there is nothing for the bot to charge.
8. **A failed charge.** The subscription goes `past_due` and access stays until the end of
   the grace period. Retries run at +1 and +3 days with attempt numbers 1 and 2, each with
   its own key. The member gets "Не получилось списать …" with a "pay" button, which opens a
   checkout for the same period. If both the retry and the button payment go through, the
   period is extended once, and the second payment is flagged for the owner to refund.
   YooKassa answers 4xx other than 429 are final refusals. 401 and 403 also alert the owner,
   because only the owner can fix the shop's credentials or the shop's permissions. Code:
   [`scheduler.py`](src/paidclub/scheduler.py).
9. **Expiry and removal.** When grace ends with nothing paid, the subscription becomes
   `expired` and the member gets "доступ к клубу закрыт" with a button to choose a plan. In
   the same scheduler pass the bot removes them from every club chat with `getChatMember`,
   `banChatMember(until_date=now + 10 min)` and `unbanChatMember(only_if_banned=True)`. Once
   the person pays again, the same link works for them. Code: [`access.py`](src/paidclub/access.py),
   [`bot/revoker.py`](src/paidclub/bot/revoker.py).
10. **Cancelling.** "Отключить автопродление" moves the subscription to `canceled`. Access
    lasts until the period end, then the subscription expires and the member is removed as
    above. "Включить автопродление" before that undoes it. For Stars, cancelling calls
    `editUserStarSubscription(is_canceled=True)` first. Turning renewal back on calls it with
    `is_canceled=False`, which only allows the member to re-enable the subscription in
    Telegram, so the bot tells them where to do that.

The state machine, the tables, one scheduler pass and every idempotency key are described in
[docs/architecture.md](docs/architecture.md).

## Providers

| | YooKassa | Telegram Stars | Fake (demo) |
| --- | --- | --- | --- |
| Currency | RUB, from `price_rub` | XTR, from `price_stars` | RUB, from `price_rub` |
| Plans | Any period | 30 days only: `subscription_period` must be 2592000, so only 1-month and 30-day plans can have `price_stars` | Any period |
| Price limit | none in this code | at most 10000 Stars per period (Bot API) | none |
| First payment | YooKassa's page (`confirmation.type = redirect`), card saved with `save_payment_method: true` | Invoice link opened in Telegram | Local page `/fake-checkout/<token>` with four outcomes |
| Renewals | Charged by the app: `payment_method_id` plus `Idempotence-Key` | Charged by Telegram, arrive as `successful_payment` with `is_recurring` | Charged by the app against a "saved card" that either always pays or always declines |
| How a payment is confirmed | Body not trusted: `GET /v3/payments/{id}` with the shop credentials | Bot updates from Telegram, `pre_checkout_query` checked first | HMAC-SHA256 signature in `X-Fake-Signature` |
| Missed or failed notification | Payment looked up by id for up to 48 hours | The update is stored before it is applied, and the scheduler retries one that failed to apply. Stars payments are not looked up | The page sends its own notification |
| Failed renewal | Retried 1 and 3 days after the first failure | Telegram sends nothing. A renewal missing 60 minutes after the period end counts as failed | Retried like YooKassa |
| Cancel | Our side only: there is no subscription at YooKassa | `editUserStarSubscription(is_canceled=True)` | Our side only |
| Refunds | Made by the owner in the YooKassa dashboard. `refund.succeeded` is confirmed with `GET /v3/refunds/{id}`, then auto-renew is turned off and the card forgotten | Not made by the app (`refundStarPayment` is not called). The owner is alerted when a payment needs one | None |
| What the shop needs | Shop id and secret key. **Autopayments work by default only in a test shop; a live shop needs a YooKassa manager to enable them.** Notifications go only to HTTPS on port 443 or 8443 | Nothing: Stars go to the bot's balance | Nothing. No money moves, and anyone with a link can "pay" |

The fake provider is refused when `PAIDCLUB_ENV=prod`. `paidclub run` and `paidclub web` also
refuse it next to a real provider, or with club chats on a public `PAIDCLUB_PUBLIC_BASE_URL`,
unless `PAIDCLUB_DEMO=1` says it is a demo on purpose.

All three implement one protocol in [`billing/base.py`](src/paidclub/billing/base.py): start a
checkout, charge a renewal, look a payment up, parse and verify a webhook, cancel, resume.
None of them touches the database. Everything they report goes through
`apply_payment_event` in [`payments.py`](src/paidclub/payments.py), which is the one place
where money changes a subscription.

Prodamus is not included. I could not confirm from Prodamus's official documentation or its
official libraries how its notifications are signed, and this code does not apply a payment
notification it cannot verify.

## Quick start

You need Python 3.12 or newer. `make install` creates `.venv` with
`python3 -m venv .venv` and installs the package with `.venv/bin/pip install -e ".[dev]"`.

### The dashboard on simulated data, no Telegram needed

```sh
git clone https://github.com/sinnercode228/tg-paid-club
cd tg-paid-club
make install
make demo     # fills demo.db with 120 simulated days, then serves http://127.0.0.1:8000/admin
```

Log in as `admin` with the password `demo`: the admin token also works as the basic auth
password. `make demo` runs these two commands:

```sh
.venv/bin/paidclub simulate --db demo.db --reset
PAIDCLUB_DATABASE_URL=sqlite+aiosqlite:///demo.db PAIDCLUB_ADMIN_TOKEN=demo .venv/bin/paidclub web
```

`paidclub simulate` runs the real code on a fake clock. Members check out through the fake
provider's signed webhook, the scheduler charges renewals, retries declined cards, expires
subscriptions and "removes" members through a stand-in that only records the call, and the
outbox "sends" messages to a stand-in that only counts them. Nothing talks to Telegram or a
payment system. The members' behaviour is random but reproducible with `--seed`. Some cancel,
resume, pay while past due or come back after their access ended. Some pay without saving the
card, have a card that declines every renewal, or have blocked the bot. The simulated period
ends at the current hour, so "the last 30 days" on the dashboard are its last 30 days. Other
options: `--days`, `--members`, `--tick-hours`, `--failure-rate`. It takes about 20 seconds
and prints a summary like this one (simulated, from the run behind the screenshot):

```text
Simulated 120 days (2026-06-27 03:00 to 2026-10-25 03:00 UTC) into …/demo.db
members: 300 arrived, 281 subscribed, 23 checkouts declined, 18 abandoned
members' actions: 40 cancels, 19 resumes, 19 paid while past due, 4 came back
outbox: 991 sent, 17 undeliverable; scheduler ticks: 961
now: 264 live (254 active, 2 past due, 8 canceled), MRR 233051.67 RUB
last 30 days: 82 new, 105 renewals, 7 failed charges, 12 churned, revenue 333370.00 RUB
```

The same numbers as JSON, and the CSV exports:

```sh
curl -H "Authorization: Bearer demo" "http://127.0.0.1:8000/admin/stats.json?days=7"
curl -u admin:demo -o payments.csv http://127.0.0.1:8000/admin/export/payments.csv
```

### The bot with the fake provider

You need a bot token and a chat to sell access to.

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a private channel or a supergroup and add the bot as an administrator with the right
   to invite users via link. In a supergroup, also give it the right to ban users.
3. Find two ids. In [Telegram Web](https://web.telegram.org/a/) (the `/a/` version) the
   address bar shows the open chat's id after `#`: your own id in Saved Messages, `-100…` for
   the channel or group.
4. `cp .env.example .env` and set `PAIDCLUB_BOT_TOKEN`, `PAIDCLUB_OWNER_ID`,
   `PAIDCLUB_CLUB_CHAT_IDS`, `PAIDCLUB_ADMIN_TOKEN` and `PAIDCLUB_FAKE_WEBHOOK_SECRET`. For the
   two secrets, use `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`.
   `PAIDCLUB_PROVIDERS=fake` is already in the file.
5. `make check` prints what `run` would use and every problem with the settings, each named by
   its variable. Then `make run`.

The bot polls Telegram, so no public address is needed. In the bot, `/start`, pick a plan and
"Тестовая оплата". The payment page opens at `http://localhost:8000/fake-checkout/…`, in a
browser on the same computer. If Telegram refuses a `localhost` link as a button, the bot
puts the link into the message text instead. Choose an outcome, and the bot sends the join
button. A second Telegram account that follows the link without paying is declined and gets
the plans. `/stats`, `/export` and http://127.0.0.1:8000/admin show what happened.

Renewals of the default plans come a month later. To watch a reminder, a renewal and dunning
within a day or two, add these to `.env`:

```sh
PAIDCLUB_PLANS='[{"code": "day", "title": "1 день", "period_unit": "day", "period_count": 1, "price_rub": "10.00"}]'
PAIDCLUB_REMINDER_DAYS=0.5
PAIDCLUB_GRACE_DAYS=1
PAIDCLUB_DUNNING_RETRY_DAYS=0.25,0.5
```

On the payment page, "Оплатить картой, которая откажет при продлении" gives a card whose
renewals are always declined.

### With YooKassa or Stars

- YooKassa: set `PAIDCLUB_YOOKASSA_SHOP_ID`, `PAIDCLUB_YOOKASSA_SECRET_KEY` and
  `PAIDCLUB_YOOKASSA_WEBHOOK_TOKEN`, put `yookassa` in `PAIDCLUB_PROVIDERS` and the public
  HTTPS address in `PAIDCLUB_PUBLIC_BASE_URL`. In the shop settings, set the notification URL
  to `{PAIDCLUB_PUBLIC_BASE_URL}/webhooks/yookassa/<token>` (`paidclub check` prints this URL) with
  the events `payment.succeeded`, `payment.canceled` and `refund.succeeded`. A test shop has
  autopayments on. A live shop gets them only after a YooKassa manager enables them.
- Stars: put `stars` in `PAIDCLUB_PROVIDERS`. The default monthly plan has `price_stars: 500`.
  No credentials are needed.

`PAIDCLUB_PROVIDERS=yookassa,stars` offers both, in that order.

## Configuration

Every setting is an environment variable with the `PAIDCLUB_` prefix, read from the
environment or from `.env` in the working directory. The environment wins over the file. An
empty value means "not set". Lists take `1,3` or `[1, 3]`. The table follows
[`config.py`](src/paidclub/config.py), and [`.env.example`](.env.example) has the same list
with comments. `paidclub check` validates the result and names the variable behind every
problem.

| Variable | Default | What it does |
| --- | --- | --- |
| `PAIDCLUB_ENV` | `dev` | `dev`, `test` or `prod`. `prod` refuses the fake provider, hides `/docs` and refuses an admin token or password shorter than 16 characters. The Docker image sets `prod`. |
| `PAIDCLUB_DEMO` | `0` | `1` allows the fake provider next to a real one, or for club chats on a public address. |
| `PAIDCLUB_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `PAIDCLUB_DISPLAY_TIMEZONE` | `Europe/Moscow` | Time zone of dates in messages and of days on the dashboard. Storage is always UTC. |
| `PAIDCLUB_DATABASE_URL` | `sqlite+aiosqlite:///./paidclub.db` | SQLite or `postgresql+asyncpg://…`. `postgresql://` and `sqlite://` are switched to the async drivers. |
| `PAIDCLUB_BOT_TOKEN` | not set | Token from @BotFather. Required by `paidclub run`. |
| `PAIDCLUB_OWNER_ID` | not set | The owner's Telegram user id: `/stats`, `/export` and alerts go here, and the owner is let into club chats and never removed. |
| `PAIDCLUB_ADMIN_IDS` | empty | Other user ids that are let into club chats without a subscription and never removed (moderators, co-authors). |
| `PAIDCLUB_CLUB_CHAT_IDS` | empty | The channel and/or supergroup ids (`-100…`) a subscription opens. |
| `PAIDCLUB_BOT_MODE` | `polling` | `polling`, or `webhook`: Telegram posts updates to `{PAIDCLUB_PUBLIC_BASE_URL}/telegram/webhook`, which must be HTTPS on port 443, 80, 88 or 8443. |
| `PAIDCLUB_TELEGRAM_WEBHOOK_SECRET` | not set | Required in webhook mode: 1–256 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`. Telegram sends it back with every update. |
| `PAIDCLUB_WEB_HOST` | `127.0.0.1` | Address the web app listens on. The Docker image uses `0.0.0.0`. |
| `PAIDCLUB_WEB_PORT` | `8000` | Port the web app listens on. |
| `PAIDCLUB_PUBLIC_BASE_URL` | `http://localhost:8000` | The address members and providers reach the web app at: checkout links, the page after payment, payment notifications, the Telegram webhook. |
| `PAIDCLUB_ADMIN_TOKEN` | not set | Opens `/admin` with `Authorization: Bearer <token>`. In a browser, log in as `PAIDCLUB_ADMIN_USERNAME` with the token as the password. |
| `PAIDCLUB_ADMIN_USERNAME` | `admin` | User name for basic auth on `/admin`. |
| `PAIDCLUB_ADMIN_PASSWORD` | not set | Basic auth password for `/admin`. With neither it nor the token set, `/admin` answers 404. |
| `PAIDCLUB_CHECKOUT_RETURN_URL` | not set | Where a provider sends the member after paying. The default is `{PAIDCLUB_PUBLIC_BASE_URL}/paid`, a page that sends them back to the bot and promises nothing about the payment. |
| `PAIDCLUB_PLANS` | `month` 990 ₽, 500 ⭐ or 10 $, `quarter` 2490 ₽ or 25 $, `year` 8900 ₽ or 90 $ | JSON list of plans: `code`, `title`, `period_unit` (`month` or `day`), `period_count` (1–36), `price_rub` (a string such as `"990.00"`), `price_stars` (1-month or 30-day plans only), `price_usd` (a string such as `"10.00"`, for crypto). |
| `PAIDCLUB_PROVIDERS` | `fake` | `yookassa`, `stars` and/or `fake`, in the order members see them. |
| `PAIDCLUB_GRACE_DAYS` | `3` | How long access lasts after a failed renewal while retries run. |
| `PAIDCLUB_DUNNING_RETRY_DAYS` | `1,3` | Retries, in days after the first failure. Ascending, and the last one inside the grace period. |
| `PAIDCLUB_REMINDER_DAYS` | `3,1` | Reminders, in days before a renewal or the end of access. |
| `PAIDCLUB_STARS_RENEWAL_WAIT_MINUTES` | `60` | How long after the period end a Stars renewal is waited for before it counts as failed. |
| `PAIDCLUB_FAKE_WEBHOOK_SECRET` | random at every start | Signs the fake provider's checkout links and webhooks. Set it, or links from before a restart stop working. |
| `PAIDCLUB_YOOKASSA_SHOP_ID` | not set | Shop id from the YooKassa dashboard. |
| `PAIDCLUB_YOOKASSA_SECRET_KEY` | not set | Secret key from the YooKassa dashboard. |
| `PAIDCLUB_YOOKASSA_API_URL` | `https://api.yookassa.ru/v3` | YooKassa API base URL. |
| `PAIDCLUB_YOOKASSA_WEBHOOK_TOKEN` | not set | 16–128 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`. When set, notifications are accepted only at `/webhooks/yookassa/<token>`, and anything posted elsewhere gets 404 before any request to the shop API. |
| `PAIDCLUB_SCHEDULER_INTERVAL_SECONDS` | `30` | Time between scheduler passes. |
| `PAIDCLUB_CLAIM_LEASE_SECONDS` | `300` | How long a worker owns a claimed renewal, removal or message before another worker may take it over. |
| `PAIDCLUB_PROVIDER_RETRY_SECONDS` | `300` | After a provider error (timeout, 5xx), when the same charge is tried again. |
| `PAIDCLUB_PENDING_RECHECK_SECONDS` | `900` | How often a charge the provider reports as pending is looked up. |
| `PAIDCLUB_OUTBOX_BATCH_SIZE` | `20` | Messages claimed per outbox round. |
| `PAIDCLUB_OUTBOX_MAX_ATTEMPTS` | `8` | Attempts before a message that keeps failing is marked `failed`. Waits start at 30 seconds and double, up to an hour. |

`POSTGRES_PASSWORD` is read by `docker-compose.yml` only.

## Docker

```sh
cp .env.example .env   # bot token, owner, club chats, admin token, yookassa and/or stars
docker compose up --build
```

Compose starts PostgreSQL 17 and the app, and passes the app
`postgresql+asyncpg://paidclub:…@db:5432/paidclub`. The image runs as a non-root user with
`PAIDCLUB_ENV=prod`, so it stops with "the fake provider is for demos and tests; disable it in
prod" while `.env` still has `PAIDCLUB_PROVIDERS=fake`. That value is left in `.env.example`
on purpose, so the local demo works as is. For a demo in Docker, set `PAIDCLUB_ENV=dev` in
`.env`. The web app is published on `127.0.0.1:8000` only. Put an HTTPS reverse proxy in front
of it for YooKassa notifications, the Telegram webhook mode and the `/admin` login.

Tables are created on start (`create_all`), so there is nothing to migrate on a fresh database.
`make up` and `make down` wrap the compose commands. CI builds the image and checks that it
runs as a non-root user.

## Tests

```sh
make test            # pytest on SQLite
make lint            # ruff check, ruff format --check, mypy --strict
make test-postgres   # the same tests on PostgreSQL; every table in PG_TEST_URL is dropped
```

506 tests, about 20 seconds on SQLite, 93% line and branch coverage. Each test gets its own
SQLite file. With `PAIDCLUB_TEST_DATABASE_URL` pointing at a PostgreSQL database whose name
contains `test`, the database tests run there instead, so `SELECT … FOR UPDATE SKIP LOCKED`
and `ON CONFLICT` run on the real thing. The default URL of `make test-postgres` is
`postgresql+asyncpg://paidclub:paidclub@localhost:5432/paidclub_test`. Time comes from a
`FakeClock` that moves only when a test moves it, so a test can walk a subscription through a
month, its renewal, a declined card, both retries and the end of grace without sleeping.

[`ci.yml`](.github/workflows/ci.yml) runs ruff and mypy, the tests on Python 3.12 and 3.13
with coverage, `paidclub check` on `.env.example`, the tests on PostgreSQL 17, and the Docker
build. [`tests/test_docs.py`](tests/test_docs.py) fails when the configuration tables in the two
READMEs miss a setting or show a wrong default, or when a link or a test named in the docs does not
exist.

Nothing in the tests talks to Telegram or YooKassa. [`tests/botapi.py`](tests/botapi.py) is a
Bot API session that answers from memory. Its results and errors go through aiogram's own
response parsing, so handlers see the same objects and exceptions as in production, including
`TelegramRetryAfter`. The YooKassa tests run against an in-memory API behind
`httpx.MockTransport`, built from YooKassa's documentation.

Tests worth reading before the code:

- `tests/test_scheduler.py::test_a_worker_that_dies_mid_charge_is_replaced_without_a_double_charge`
  and `test_two_workers_charge_each_renewal_exactly_once`.
- `tests/test_payments.py::test_the_same_webhook_delivered_concurrently_is_applied_once`.
- `tests/test_billing_yookassa.py::test_the_webhook_body_is_not_trusted` and
  `test_a_renewal_whose_answer_was_lost_is_looked_up_once_the_key_may_be_forgotten`.
- `tests/test_cancel_during_renewal.py`: a renewal charge approved after the member cancelled.
- `tests/test_bot_revoker.py::test_a_removal_whose_unban_was_lost_is_finished_on_retry`.
- `tests/test_outbox.py::test_two_senders_running_together_send_each_message_once`.

## Limitations

Things this code does not do, or that need care before a real club runs on it.

- **YooKassa autopayments must be enabled for the shop.** They work by default only in a test
  shop. In a live shop a YooKassa manager has to turn them on. Until then, a renewal cannot
  be charged from a saved card: it fails like a declined card, and the member can only pay
  with the button.
- **YooKassa notifications need HTTPS on port 443 or 8443** and are retried for 24 hours.
  Missed ones are caught by looking the payment up, for up to 48 hours after the checkout.
  For a proxy allowlist, YooKassa sends notifications from `185.71.76.0/27`, `185.71.77.0/27`,
  `77.75.153.0/25`, `77.75.156.11`, `77.75.156.35`, `77.75.154.128/25` and `2a02:5180::/32`
  (from YooKassa's webhook documentation).
- **No fiscal receipts.** Payments carry no `receipt` object. A shop that issues 54-FZ
  receipts through YooKassa has to send receipt data (items, the customer's contact) with
  every payment and refund, and this code does not.
- **One `paidclub run` per bot token.** Telegram gives updates to one polling client per bot,
  and two processes starting at once could each create a join-request link. The scheduler and
  the outbox are safe in several workers on one database, but there is no command that runs
  them without the bot.
- **No migrations.** `create_all` creates missing tables and never changes existing ones. A
  schema change on a live database needs a migration written by hand, or Alembic.
- **Members who were in the chat before the bot are not removed.** The bot removes members
  whose subscription expired and people who join without access. Anyone who was already in the
  chat stays. Revoke the invite links made before the bot, the primary one included, and turn
  off "add members" for members. The bot reports a public username and that permission, but
  changes neither.
- **Stars.** Monthly only. A failed renewal produces no update, so it is noticed 60 minutes
  after the period end. Stars refunds are left to the owner. Turning renewal back on needs the
  member to do it in Telegram.
- **Refunds and revenue.** A refund made in the YooKassa dashboard turns auto-renew off and
  forgets the card, and access lasts to the period end. Revenue on the dashboard is gross:
  refunds are recorded but not subtracted.
- **`/admin` does not limit login attempts.** `PAIDCLUB_ENV=prod` refuses a token or
  password shorter than 16 characters. Add a rate limit or an IP allowlist at the proxy, and
  serve it over HTTPS, since basic auth sends the password with every request.
- **At-least-once in one spot.** A sender that crashes after Telegram accepted a message, but
  before it recorded that, sends the message again once its lease runs out.
- **Prodamus is not included**, see [Providers](#providers).
- **The club's numbers in this README are simulated.** The screenshot and the `simulate`
  output come from the fake provider on a fake clock.

Before going live: set `PAIDCLUB_ENV=prod` (the Docker image already does), put HTTPS in
front, take `fake` out of `PAIDCLUB_PROVIDERS`, set `PAIDCLUB_YOOKASSA_WEBHOOK_TOKEN`, and run
`paidclub check`.

## Where is what

```
src/paidclub/
  domain/subscriptions.py  state machine, period math, dunning schedule: pure functions
  payments.py              apply_payment_event: the one place where money changes state
  scheduler.py             renewals, pending charges, expiry, removal, reminders
  notifications.py         outbox: enqueue in the same transaction, send once with a lease
  access.py                join-request decisions, removal of expired members
  checkouts.py             looking up checkouts whose notification did not arrive
  billing/                 provider protocol, YooKassa, Telegram Stars, fake provider
  bot/                     aiogram routers: member menu, join requests, chats, owner commands
  web/                     FastAPI: webhooks, fake checkout page, /admin, /paid, Telegram webhook
  stats.py                 every number on /stats and /admin, CSV export
  simulate.py              paidclub simulate
  runner.py, cli.py        one process: bot, web, scheduler, outbox; the paidclub command
  models.py, db.py         tables, constraints, claims and transaction retries
tests/                     pytest, with a fake Bot API session and a mocked YooKassa API
docs/architecture.md       state machine, tables, scheduler pass, idempotency keys
```

---

Built by Грешный Котик (sinnercode). I take freelance work like this: Telegram [@sinnercode](https://t.me/sinnercode). License: [MIT](LICENSE).
