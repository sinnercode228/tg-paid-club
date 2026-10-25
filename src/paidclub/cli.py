"""Command line entry point: ``paidclub <command>``.

* ``run``: the bot (polling or webhook), the web app, the scheduler and the outbox, in one
  process;
* ``web``: the web app alone (dashboard, checkout page, payment webhooks), no bot;
* ``check``: validate the settings and print what ``run`` would use;
* ``init-db``: create missing tables (``run`` does it too);
* ``simulate``: fill a SQLite file with simulated club activity for the dashboard.

Exit status: 0 on success, 1 when the process failed at run time, 2 when the settings are
invalid or incomplete (with one line per problem, named by its environment variable).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from typing import Any

from aiogram.exceptions import TelegramUnauthorizedError

from paidclub import runner, simulate
from paidclub.checks import (
    Command,
    ConfigError,
    describe,
    load_settings,
    safe_database_url,
    startup_problems,
)
from paidclub.config import Settings
from paidclub.db import create_all, create_engine

log = logging.getLogger("paidclub")

EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130


async def _init_db(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    try:
        await create_all(engine)
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paidclub",
        description="Paid Telegram club: subscriptions, access by join request, dashboard.",
        epilog="Settings come from PAIDCLUB_* environment variables and ./.env (see .env.example).",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    run = commands.add_parser(
        "run", help="run the bot, web app, scheduler and outbox in one process"
    )
    run.add_argument(
        "--mode", choices=["polling", "webhook"], help="override PAIDCLUB_BOT_MODE for this run"
    )
    commands.add_parser(
        "web", help="serve only the web app (dashboard, checkout page, payment webhooks)"
    )
    commands.add_parser("check", help="validate the settings and show what run would use")
    commands.add_parser("init-db", help="create missing database tables")
    simulate.configure_parser(
        commands.add_parser("simulate", help="fill a SQLite file with simulated club activity")
    )
    return parser


def _print_config_error(lines: Sequence[str]) -> None:
    print("Invalid settings (PAIDCLUB_* environment variables and .env):", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)


def _preflight(settings: Settings, command: Command) -> bool:
    """Log warnings; print errors and return False when ``command`` cannot start."""
    problems = startup_problems(settings, command)
    for problem in problems:
        if problem.severity == "warning":
            log.warning("%s", problem.message)
    errors = [problem.message for problem in problems if problem.severity == "error"]
    if errors:
        print(f"paidclub {command} cannot start:", file=sys.stderr)
        for message in errors:
            print(f"  {message}", file=sys.stderr)
    return not errors


def _serve(settings: Settings, *, with_bot: bool) -> int:
    try:
        asyncio.run(runner.main(settings, with_bot=with_bot))
    except TelegramUnauthorizedError:
        print("Telegram rejected PAIDCLUB_BOT_TOKEN: check it with @BotFather", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:  # a second Ctrl+C during shutdown
        return EXIT_INTERRUPTED
    except Exception:
        log.exception("paidclub stopped with an error")
        return EXIT_FAILED
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict[str, Any] = {}
    if getattr(args, "mode", None):
        overrides["bot_mode"] = args.mode
    try:
        settings = load_settings(**overrides)
    except ConfigError as exc:
        _print_config_error(exc.lines)
        return EXIT_CONFIG
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if args.command == "run":
        if not _preflight(settings, "run"):
            return EXIT_CONFIG
        return _serve(settings, with_bot=True)
    if args.command == "web":
        if not _preflight(settings, "web"):
            return EXIT_CONFIG
        return _serve(settings, with_bot=False)
    if args.command == "check":
        for line in describe(settings):
            print(line)
        problems = startup_problems(settings, "run")
        for problem in problems:
            print(f"{problem.severity}: {problem.message}")
        if any(problem.severity == "error" for problem in problems):
            return EXIT_CONFIG
        print("settings are valid")
        return 0
    if args.command == "init-db":
        asyncio.run(_init_db(settings))
        print(f"tables are in place in {safe_database_url(settings.database_url)}")
        return 0
    if args.command == "simulate":
        return simulate.run_cli(args, settings)
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
