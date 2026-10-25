"""``paidclub simulate``: fill a SQLite database with a few months of club activity, so the
dashboard and ``/stats`` have something realistic to show.

It runs the real code paths on fake time. Members check out through the fake provider's signed
webhook, exactly like the local payment page does; the scheduler charges renewals, retries
declined cards, expires subscriptions after the grace period and "removes" members through a
revoker that only records the call; the outbox "sends" every message through a sender that
only counts it. Nothing talks to Telegram or a payment system, and no time is spent waiting.

What the simulated members do is random but reproducible (``--seed``): they arrive over the
whole run with the club growing, mostly pick the monthly plan, sometimes have a card that
declines renewals or do not let the card be saved, cancel and resume auto-renew, pay with the
"pay" button while past due, and now and then come back after their access ended.

The simulated period ends at the moment the command runs, so "the last 30 days" on the
dashboard is the end of the simulation. It refuses to write into a database that already has
members, and the command refuses to touch an existing file without ``--reset``.
"""

from __future__ import annotations

import argparse
import asyncio
import heapq
import itertools
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from paidclub import club
from paidclub.access import RevokeOutcome
from paidclub.billing import ProviderRegistry
from paidclub.billing.base import PaymentSucceeded
from paidclub.billing.fake import CardKind, FakeProvider
from paidclub.clock import FakeClock
from paidclub.config import PlanConfig, Settings
from paidclub.container import Core, build_core
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import Member, PaymentOutcome, Subscription
from paidclub.notifications import OutboxSender, OutgoingMessage, Undeliverable
from paidclub.payments import apply_event
from paidclub.scheduler import Scheduler
from paidclub.stats import Report, club_report

log = logging.getLogger(__name__)

#: Simulated members get Telegram ids from here up, far from real ids of the owner and admins.
TELEGRAM_ID_BASE = 9_000_000_000
# fmt: off
_FIRST_NAMES = (
    "Анна", "Мария", "Елена", "Ольга", "Дарья", "Алина", "Ксения", "Виктория", "Наталья", "Юлия",
    "Алексей", "Дмитрий", "Иван", "Максим", "Сергей", "Андрей", "Павел", "Никита", "Егор", "Артём",
)
# fmt: on


class SimulationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Behaviour:
    """Probabilities that drive the simulated members. Rates "per day" apply to each member."""

    #: Pays without letting the card be saved: every renewal fails until they pay by hand.
    no_saved_card: float = 0.12
    #: Saves a card that the bank declines on every renewal.
    declining_card: float = 0.05
    #: The first card is declined on the checkout page...
    checkout_declined: float = 0.08
    #: ...and of those, this share tries again with another card a few minutes later.
    retry_after_decline: float = 0.6
    #: Opens the checkout page and never pays.
    abandons_checkout: float = 0.06
    cancel_per_day: float = 0.004
    resume_per_day: float = 0.03
    #: Pays with the "pay" button while a renewal is failing.
    pays_when_past_due_per_day: float = 0.35
    #: Comes back after access ended (or after an abandoned checkout).
    comes_back_per_day: float = 0.004
    #: Has blocked the bot: messages to them end up undeliverable.
    blocks_bot: float = 0.03
    #: Relative popularity of the plans, in the configured order; extra plans get the last.
    plan_weights: tuple[float, ...] = (0.62, 0.25, 0.13)
    #: Arrivals per day grow linearly from 1 to this factor over the run.
    growth: float = 3.0


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    days: int = 120
    members: int = 300
    seed: int = 7
    #: Defaults to ``days`` before the current hour, so the simulation ends "now".
    start: datetime | None = None
    tick: timedelta = timedelta(hours=3)
    #: Share of renewal charges that the fake bank declines on otherwise good cards.
    renewal_failure_rate: float = 0.08
    behaviour: Behaviour = field(default_factory=Behaviour)

    def __post_init__(self) -> None:
        if self.days < 1:
            raise SimulationError("days must be at least 1")
        if self.members < 0:
            raise SimulationError("members must not be negative")
        if self.tick <= timedelta(0):
            raise SimulationError("tick must be positive")
        if not 0 <= self.renewal_failure_rate <= 1:
            raise SimulationError("renewal_failure_rate must be between 0 and 1")


@dataclass
class SimulationResult:
    start: datetime
    end: datetime
    arrived: int = 0
    subscribed: int = 0
    checkouts_declined: int = 0
    abandoned: int = 0
    cancels: int = 0
    resumes: int = 0
    repayments: int = 0
    comebacks: int = 0
    skipped_actions: int = 0
    ticks: int = 0
    messages_sent: int = 0
    messages_undeliverable: int = 0
    report: Report | None = None


Action = Callable[[], Awaitable[None]]


class _RecordingRevoker:
    """Stands in for the bot: every expired member is "removed" from the club chats."""

    def __init__(self) -> None:
        self.calls = 0

    async def revoke(self, chat_id: int, user_id: int) -> RevokeOutcome:
        self.calls += 1
        return RevokeOutcome.REMOVED


class _Simulation:
    def __init__(
        self,
        core: Core,
        clock: FakeClock,
        fake: FakeProvider,
        config: SimulationConfig,
        plans: Sequence[PlanConfig],
        result: SimulationResult,
    ) -> None:
        self.core = core
        self.clock = clock
        self.fake = fake
        self.config = config
        self.behaviour = config.behaviour
        self.plans = list(plans)
        # Reproducible member behaviour, not anything secret.
        self.rng = random.Random(config.seed)  # noqa: S311
        self.result = result
        self.blocked: set[int] = set()
        self.queue: list[tuple[datetime, int, Action]] = []
        self.order = itertools.count()
        self.scheduler = Scheduler(core, _RecordingRevoker())
        self.sender = OutboxSender(
            core.sessions,
            clock,
            self._send,
            batch_size=200,
            lease=core.settings.claim_lease,
            max_attempts=core.settings.outbox_max_attempts,
        )
        weights = list(self.behaviour.plan_weights) or [1.0]
        self.plan_weights = [weights[min(i, len(weights) - 1)] for i in range(len(self.plans))]

    # --- the event queue -------------------------------------------------------------------

    def at(self, moment: datetime, action: Action) -> None:
        heapq.heappush(self.queue, (moment, next(self.order), action))

    def later(self, delay: timedelta, action: Action) -> None:
        self.at(self.clock.now() + delay, action)

    def random_time(self, day_start: datetime) -> datetime:
        return day_start + timedelta(seconds=self.rng.uniform(0, 86_400))

    async def run(self, start: datetime, end: datetime) -> None:
        days = self.config.days
        day_weights = [1 + (self.behaviour.growth - 1) * d / max(days - 1, 1) for d in range(days)]
        for number in range(self.config.members):
            day = self.rng.choices(range(days), weights=day_weights)[0]
            self.at(self.random_time(start + timedelta(days=day)), self._arrival(number))
        for day in range(1, days):
            self.at(start + timedelta(days=day), self._daily_roll)
        moment = start
        while moment < end:
            self.at(moment, self._tick)
            moment += self.config.tick

        while self.queue:
            moment, _, action = heapq.heappop(self.queue)
            if moment >= end:
                break
            self.clock.advance(max(moment - self.clock.now(), timedelta(0)))
            await action()
        # One last scheduler pass at the end, so the database is settled at that moment.
        self.clock.advance(end - self.clock.now())
        await self._tick()

    # --- periodic work ---------------------------------------------------------------------

    async def _tick(self) -> None:
        report = await self.scheduler.tick()
        self.result.ticks += 1
        if report.errors:
            log.warning("simulated tick at %s had %s errors", self.clock.now(), report.errors)
        while True:
            sent = await self.sender.run_once()
            self.result.messages_sent += sent.sent
            self.result.messages_undeliverable += sent.undeliverable
            if sent.claimed == 0:
                break

    async def _send(self, message: OutgoingMessage) -> None:
        if message.chat_id in self.blocked:
            raise Undeliverable("Forbidden: bot was blocked by the user")

    # --- members ---------------------------------------------------------------------------

    def _pick_plan(self) -> PlanConfig:
        return self.rng.choices(self.plans, weights=self.plan_weights)[0]

    def _pick_card(self) -> CardKind | None:
        roll = self.rng.random()
        if roll < self.behaviour.no_saved_card:
            return None
        if roll < self.behaviour.no_saved_card + self.behaviour.declining_card:
            return "decline"
        return "ok"

    def _arrival(self, number: int) -> Action:
        async def arrive() -> None:
            telegram_id = TELEGRAM_ID_BASE + number
            self.result.arrived += 1
            if self.rng.random() < self.behaviour.blocks_bot:
                self.blocked.add(telegram_id)
            first_name = self.rng.choice(_FIRST_NAMES)
            username = f"sim_member_{number:04d}" if self.rng.random() < 0.7 else None
            await self._new_checkout(telegram_id, username=username, first_name=first_name)

        return arrive

    async def _new_checkout(
        self, telegram_id: int, *, username: str | None = None, first_name: str | None = None
    ) -> None:
        plan = self._pick_plan()
        try:
            link = await club.start_checkout(
                self.core,
                telegram_id=telegram_id,
                plan_code=plan.code,
                provider=self.fake.name,
                username=username,
                first_name=first_name,
            )
        except club.ClubError:
            self.result.skipped_actions += 1
            return
        roll = self.rng.random()
        if roll < self.behaviour.abandons_checkout:
            self.result.abandoned += 1
            return
        card = self._pick_card()
        if roll < self.behaviour.abandons_checkout + self.behaviour.checkout_declined:
            await self._pay(link.url, succeeded=False, card=None)
            self.result.checkouts_declined += 1
            if self.rng.random() < self.behaviour.retry_after_decline:
                retry_in = timedelta(minutes=self.rng.uniform(2, 40))
                self.later(retry_in, self._retry_checkout(link.url, card))
            return
        if await self._pay(link.url, succeeded=True, card=card):
            self.result.subscribed += 1

    def _retry_checkout(self, url: str, card: CardKind | None) -> Action:
        async def retry() -> None:
            if await self._pay(url, succeeded=True, card=card):
                self.result.subscribed += 1

        return retry

    async def _pay(self, url: str, *, succeeded: bool, card: CardKind | None) -> bool:
        """What the fake payment page does: a signed webhook, verified and applied."""
        session = self.fake.read_checkout(url.rsplit("/", 1)[1])
        headers, body = self.fake.checkout_webhook(session, succeeded=succeeded, card=card)
        applied = False
        for event in await self.fake.parse_webhook(headers, body):
            result = await apply_event(self.core, event)
            applied = applied or (
                isinstance(event, PaymentSucceeded) and result.outcome is PaymentOutcome.APPLIED
            )
        return applied

    async def _daily_roll(self) -> None:
        """Once a day, decide what each member does today and schedule it."""
        day_start = self.clock.now()
        async with self.core.sessions() as session:
            rows = await session.execute(
                select(Subscription.id, Subscription.status, Member.telegram_id)
                .join(Member, Member.id == Subscription.member_id)
                .order_by(Subscription.id)
            )
            latest: dict[int, tuple[int, SubStatus]] = {}
            for sub_id, status, telegram_id in rows:
                latest[telegram_id] = (sub_id, status)
        b = self.behaviour
        for telegram_id, (sub_id, status) in latest.items():
            roll = self.rng.random()
            moment = self.random_time(day_start)
            if status is SubStatus.ACTIVE and roll < b.cancel_per_day:
                self.at(moment, self._cancel(telegram_id, sub_id))
            elif status is SubStatus.CANCELED and roll < b.resume_per_day:
                self.at(moment, self._resume(telegram_id, sub_id))
            elif status is SubStatus.PAST_DUE and roll < b.pays_when_past_due_per_day:
                self.at(moment, self._repay(telegram_id, sub_id))
            elif status in (SubStatus.EXPIRED, SubStatus.PENDING) and roll < b.comes_back_per_day:
                self.at(moment, self._come_back(telegram_id, sub_id, status))

    def _cancel(self, telegram_id: int, sub_id: int) -> Action:
        async def cancel() -> None:
            try:
                await club.cancel(self.core, telegram_id=telegram_id, subscription_id=sub_id)
            except club.ClubError:
                self.result.skipped_actions += 1
                return
            self.result.cancels += 1

        return cancel

    def _resume(self, telegram_id: int, sub_id: int) -> Action:
        async def resume() -> None:
            try:
                await club.resume(self.core, telegram_id=telegram_id, subscription_id=sub_id)
            except club.ClubError:
                self.result.skipped_actions += 1
                return
            self.result.resumes += 1

        return resume

    def _repay(self, telegram_id: int, sub_id: int) -> Action:
        async def repay() -> None:
            try:
                link = await club.start_repayment(
                    self.core, telegram_id=telegram_id, subscription_id=sub_id
                )
            except club.ClubError:
                self.result.skipped_actions += 1
                return
            card: CardKind | None = "ok" if self.rng.random() < 0.85 else None
            if await self._pay(link.url, succeeded=True, card=card):
                self.result.repayments += 1

        return repay

    def _come_back(self, telegram_id: int, sub_id: int, status: SubStatus) -> Action:
        async def come_back() -> None:
            self.result.comebacks += 1
            if status is SubStatus.EXPIRED and self.rng.random() < 0.3:
                # An old "pay" button: a late payment on the same subscription.
                await self._repay(telegram_id, sub_id)()
            else:
                await self._new_checkout(telegram_id)

        return come_back


def default_start(days: int, *, now: datetime | None = None) -> datetime:
    """``days`` before the current hour, so the simulated period ends now."""
    now = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(days=days)


async def simulate(
    settings: Settings, config: SimulationConfig, *, database_url: str
) -> SimulationResult:
    """Run the simulation into ``database_url`` (SQLite only, and it must hold no members)."""
    if not database_url.startswith("sqlite"):
        raise SimulationError("simulate only writes to SQLite; pass a file path with --db")
    start = config.start or default_start(config.days)
    end = start + timedelta(days=config.days)
    clock = FakeClock(start)
    sim_settings = settings.model_copy(
        update={"database_url": database_url, "providers": ["fake"], "env": "dev"}
    )
    plans = [plan for plan in sim_settings.plans if plan.price_rub is not None]
    if not plans:
        raise SimulationError("the fake provider charges rubles: configure a plan with price_rub")
    fake = FakeProvider(
        secret=sim_settings.fake_webhook_secret.get_secret_value(),
        base_url=sim_settings.public_base_url,
        clock=clock,
        failure_rate=config.renewal_failure_rate,
    )
    core = await build_core(
        sim_settings, clock=clock, providers=ProviderRegistry([fake]), create_schema=True
    )
    try:
        async with core.sessions() as session:
            if await session.scalar(select(func.count()).select_from(Member)):
                raise SimulationError("the database already has members; use an empty one")
        result = SimulationResult(start=start, end=end)
        await _Simulation(core, clock, fake, config, plans, result).run(start, end)
        result.report = await club_report(core, days=min(30, config.days))
        return result
    finally:
        await core.aclose()


# --- command line --------------------------------------------------------------------------


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--db", type=Path, default=Path("demo.db"), help="SQLite file to create")
    parser.add_argument("--reset", action="store_true", help="replace the file if it exists")
    parser.add_argument("--days", type=int, default=120, help="simulated days (default 120)")
    parser.add_argument("--members", type=int, default=300, help="members who arrive")
    parser.add_argument("--seed", type=int, default=7, help="random seed")
    parser.add_argument(
        "--tick-hours", type=float, default=3, help="scheduler interval in simulated hours"
    )
    parser.add_argument(
        "--failure-rate", type=float, default=0.08, help="share of declined renewal charges"
    )
    return parser


def _remove_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def summary(result: SimulationResult, path: Path) -> str:
    lines = [
        f"Simulated {(result.end - result.start).days} days "
        f"({result.start:%Y-%m-%d %H:%M} to {result.end:%Y-%m-%d %H:%M} UTC) into {path}",
        f"members: {result.arrived} arrived, {result.subscribed} subscribed, "
        f"{result.checkouts_declined} checkouts declined, {result.abandoned} abandoned",
        f"members' actions: {result.cancels} cancels, {result.resumes} resumes, "
        f"{result.repayments} paid while past due, {result.comebacks} came back",
        f"outbox: {result.messages_sent} sent, {result.messages_undeliverable} undeliverable; "
        f"scheduler ticks: {result.ticks}",
    ]
    report = result.report
    if report is not None:
        now, totals = report.snapshot.total, report.totals
        mrr = ", ".join(f"{m.to_decimal_str()} {code}" for code, m in now.mrr.items()) or "0"
        revenue = ", ".join(f"{m.to_decimal_str()} {c}" for c, m in totals.revenue.items()) or "0"
        lines += [
            f"now: {now.live} live ({now.active} active, {now.past_due} past due, "
            f"{now.canceled} canceled), MRR {mrr}",
            f"last {len(report.days)} days: {totals.new} new, {totals.renewals} renewals, "
            f"{totals.failed_charges} failed charges, {totals.churned} churned, revenue {revenue}",
        ]
    lines.append(
        f"Dashboard: PAIDCLUB_DATABASE_URL=sqlite+aiosqlite:///{path} "
        "PAIDCLUB_ADMIN_TOKEN=<token> paidclub web, then open /admin"
    )
    return "\n".join(lines)


def run_cli(args: argparse.Namespace, settings: Settings) -> int:
    path: Path = args.db.expanduser().resolve()
    if path.exists():
        if not args.reset:
            raise SystemExit(f"{path} already exists; pass --reset to replace it")
        _remove_database(path)
    try:
        config = SimulationConfig(
            days=args.days,
            members=args.members,
            seed=args.seed,
            tick=timedelta(hours=args.tick_hours),
            renewal_failure_rate=args.failure_rate,
        )
    except SimulationError as exc:
        raise SystemExit(str(exc)) from exc
    # Every payment and tick logs at INFO; a simulation makes thousands of them.
    package_log = logging.getLogger("paidclub")
    level = package_log.level
    package_log.setLevel(max(logging.WARNING, logging.getLogger().getEffectiveLevel()))
    try:
        result = asyncio.run(simulate(settings, config, database_url=f"sqlite+aiosqlite:///{path}"))
    except SimulationError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        package_log.setLevel(level)
    print(summary(result, path))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = configure_parser(argparse.ArgumentParser(prog="paidclub simulate"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return run_cli(args, Settings())


if __name__ == "__main__":
    raise SystemExit(main())
