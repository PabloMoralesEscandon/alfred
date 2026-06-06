from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import bot
import scheduler_store


DEFAULT_POLL_SECONDS = 5.0


def log(message: str) -> None:
    print(message, flush=True)


async def deliver_message(item: dict[str, Any], *, db_path: Path) -> dict[str, Any] | None:
    try:
        await bot.send_reminder(item["chat_id"], item["message"])
    except Exception as exc:
        log(f"failed {item['id']}: {exc}")
        return scheduler_store.mark_failed(item["id"], str(exc), db_path=db_path)

    log(f"sent {item['id']}")
    return scheduler_store.mark_sent(item["id"], db_path=db_path)


async def process_due_messages(*, db_path: Path) -> int:
    processed = 0

    while True:
        item = scheduler_store.claim_due_message(
            scheduler_store.utc_now_iso(),
            db_path=db_path,
        )
        if item is None:
            return processed

        processed += 1
        await deliver_message(item, db_path=db_path)


async def run_worker(*, db_path: Path, once: bool, poll_seconds: float) -> None:
    while True:
        processed = await process_due_messages(db_path=db_path)
        if once:
            log(f"processed {processed} due message(s)")
            return

        await asyncio.sleep(poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver scheduled Telegram reminders.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=scheduler_store.DEFAULT_DB_PATH,
        help="SQLite scheduler database path.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process currently due reminders, then exit.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Seconds to wait between polling passes in loop mode.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_seconds <= 0:
        print("--poll-seconds must be greater than zero", file=sys.stderr)
        return 2

    asyncio.run(
        run_worker(
            db_path=args.db_path,
            once=args.once,
            poll_seconds=args.poll_seconds,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
