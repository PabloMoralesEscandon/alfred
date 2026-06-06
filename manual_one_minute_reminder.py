from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import bot


def default_username() -> str:
    usernames = sorted(bot.load_allowed_usernames())
    return usernames[0] if usernames else ""


def one_minute_from_now() -> str:
    due = datetime.now(timezone.utc) + timedelta(minutes=1)
    return due.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Schedule a real reminder one minute from now for manual testing."
    )
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--username", default=default_username())
    parser.add_argument("--message", default="Manual one-minute reminder test")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    due = one_minute_from_now()
    command = [
        sys.executable,
        "scheduler_tool.py",
        "add",
        "--chat-id",
        str(args.chat_id),
        "--user-id",
        str(args.user_id),
        "--username",
        args.username,
        "--due",
        due,
        "--message",
        args.message,
        "--source-request",
        "manual one-minute reminder test",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return result.returncode

    payload = json.loads(result.stdout)
    if payload.get("ok"):
        print("Run: python3 scheduler_worker.py --once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
