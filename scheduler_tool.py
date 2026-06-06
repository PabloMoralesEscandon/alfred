from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import scheduler_store


ENV_PATH = Path(__file__).resolve().parent / ".env"


class ToolError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ToolError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise ToolError("help output is not supported")
        raise ToolError((message or "").strip() or "invalid arguments")


def emit(payload: dict[str, Any], *, exit_code: int = 0) -> int:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


def parse_due(value: str) -> tuple[str, datetime]:
    try:
        due_utc = scheduler_store.normalize_utc_text(value)
        parsed = due_utc[:-1] + "+00:00" if due_utc.endswith("Z") else due_utc
        due_dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise ToolError(f"due must be an ISO-8601 timestamp with timezone: {exc}") from exc

    now = datetime.now(timezone.utc)
    if due_dt <= now:
        raise ToolError("due must be in the future")

    return due_utc, due_dt


def read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    try:
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def load_allowed_usernames() -> set[str]:
    usernames = set()
    for line in read_env_lines():
        line = line.split("#", 1)[0]
        for token in line.replace(",", " ").split():
            if token.startswith("whitelisted@"):
                username = token.removeprefix("whitelisted@").lstrip("@").strip()
                if username:
                    usernames.add(username.lower())
    return usernames


def validate_username(username: str) -> None:
    if username.lstrip("@").lower() not in load_allowed_usernames():
        raise ToolError("username is not allowed")


def validate_message(message: str) -> str:
    text = message.strip()
    if not text:
        raise ToolError("message must not be empty")
    return text


def validate_source_request(source_request: str) -> str:
    text = source_request.strip()
    if not text:
        raise ToolError("source_request must not be empty")
    return text


def add_message(args: argparse.Namespace) -> dict[str, Any]:
    validate_username(args.username)
    message = validate_message(args.message)
    source_request = validate_source_request(args.source_request)
    due_utc, _ = parse_due(args.due)

    item = scheduler_store.create_message(
        chat_id=args.chat_id,
        user_id=args.user_id,
        username=args.username,
        due_at_utc=due_utc,
        message=message,
        source_request=source_request,
        db_path=args.db_path,
    )
    return {
        "ok": True,
        "id": item["id"],
        "status": item["status"],
        "due_at_utc": item["due_at_utc"],
    }


def list_messages(args: argparse.Namespace) -> dict[str, Any]:
    items = scheduler_store.list_messages(
        args.chat_id,
        status=args.status,
        db_path=args.db_path,
    )
    return {"ok": True, "items": items}


def show_message(args: argparse.Namespace) -> dict[str, Any]:
    item = scheduler_store.get_message(args.id, db_path=args.db_path)
    if item is None:
        raise ToolError("message not found")
    return {"ok": True, "item": item}


def cancel_message(args: argparse.Namespace) -> dict[str, Any]:
    item = scheduler_store.cancel_message(
        args.id,
        args.chat_id,
        db_path=args.db_path,
    )
    if item is None:
        raise ToolError("scheduled message not found")
    return {"ok": True, "item": item}


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="scheduler_tool.py", add_help=False)
    parser.add_argument("--db-path", type=Path, default=scheduler_store.DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", add_help=False)
    add.add_argument("--chat-id", type=int, required=True)
    add.add_argument("--user-id", type=int, required=True)
    add.add_argument("--username", required=True)
    add.add_argument("--due", required=True)
    add.add_argument("--message", required=True)
    add.add_argument("--source-request", required=True)
    add.set_defaults(handler=add_message)

    list_parser = subparsers.add_parser("list", add_help=False)
    list_parser.add_argument("--chat-id", type=int, required=True)
    list_parser.add_argument("--status")
    list_parser.set_defaults(handler=list_messages)

    show = subparsers.add_parser("show", add_help=False)
    show.add_argument("--id", required=True)
    show.set_defaults(handler=show_message)

    cancel = subparsers.add_parser("cancel", add_help=False)
    cancel.add_argument("--id", required=True)
    cancel.add_argument("--chat-id", type=int, required=True)
    cancel.set_defaults(handler=cancel_message)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return emit(args.handler(args))
    except ToolError as exc:
        return emit({"ok": False, "error": str(exc)}, exit_code=1)
    except Exception as exc:
        return emit({"ok": False, "error": f"unexpected error: {exc}"}, exit_code=1)


if __name__ == "__main__":
    sys.exit(main())
