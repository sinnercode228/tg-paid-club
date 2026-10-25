"""Command line entry point: ``paidclub <command>``."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from paidclub.config import Settings
from paidclub.db import create_all, create_engine


async def _init_db(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    try:
        await create_all(engine)
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paidclub", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="create the database tables")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.command == "init-db":
        asyncio.run(_init_db(settings))
        print(f"tables created in {settings.database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
