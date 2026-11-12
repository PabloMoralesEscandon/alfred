"""The `alfred` command.

For the operator: `run`, `init`, `chat`. For the agent itself (through
`bin/alfred` in its home): `schedule`, `send`, `event`, `skills`. The
agent-facing commands print JSON so the model can read the outcome reliably.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, home
from .config import Config
from .scheduler import KIND_EVENT, KIND_MESSAGE, KIND_TASK, SchedulerStore, parse_duration, parse_time, utc_now

LOCAL_CHAT_ID = 0


class CommandError(Exception):
    pass


def emit(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ok") else 1


def resolve_chat_id(args: argparse.Namespace) -> int:
    value = args.chat_id if args.chat_id is not None else os.environ.get("ALFRED_CHAT_ID", "")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise CommandError("no chat: pass --chat-id or run inside an Alfred turn (ALFRED_CHAT_ID)") from None


def describe(item: Dict[str, Any]) -> Dict[str, Any]:
    due = parse_time(item["due_at_utc"])
    return {
        "id": item["id"],
        "kind": item["kind"],
        "text": item["message"],
        "status": item["status"],
        "due_utc": item["due_at_utc"],
        "due_local": due.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "repeat_seconds": item["repeat_seconds"],
        "last_error": item["last_error"],
    }


# -- agent-facing commands ---------------------------------------------------


def cmd_schedule_add(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    if bool(args.at) == bool(args.in_):
        raise CommandError("give exactly one of --at or --in")
    if bool(args.message) == bool(args.task):
        raise CommandError("give exactly one of --message or --task")
    try:
        due = utc_now() + timedelta(seconds=parse_duration(args.in_)) if args.in_ else parse_time(args.at)
        repeat = parse_duration(args.every) if args.every else None
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    if due <= utc_now():
        raise CommandError("that time is in the past (now is %s)" % utc_now().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
    try:
        item = SchedulerStore(config.db_path).add(
            chat_id=resolve_chat_id(args),
            due=due,
            text=args.task or args.message,
            kind=KIND_TASK if args.task else KIND_MESSAGE,
            repeat_seconds=repeat,
            user_id=int(os.environ.get("ALFRED_USER_ID") or 0),
            username=os.environ.get("ALFRED_USERNAME", ""),
        )
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    return {"ok": True, "item": describe(item)}


def cmd_schedule_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    items = SchedulerStore(config.db_path).list(resolve_chat_id(args), include_done=args.all)
    return {"ok": True, "items": [describe(item) for item in items]}


def cmd_schedule_cancel(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    item = SchedulerStore(config.db_path).cancel(args.id, resolve_chat_id(args))
    if item is None:
        raise CommandError("no single pending item matches %r; run `alfred schedule list`" % args.id)
    return {"ok": True, "item": describe(item)}


def cmd_event(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Wake the chat's agent with a note. Works from any process on this machine, not only inside a turn."""
    text = " ".join(args.text).strip()
    if not text:
        raise CommandError("nothing to report: give the text of the event")
    item = SchedulerStore(config.db_path).add(
        chat_id=resolve_chat_id(args),
        due=utc_now(),
        text=text,
        kind=KIND_EVENT,
        user_id=int(os.environ.get("ALFRED_USER_ID") or 0),
        username=os.environ.get("ALFRED_USERNAME", ""),
    )
    return {"ok": True, "item": describe(item)}


def cmd_send(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    chat_id = resolve_chat_id(args)
    text = " ".join(args.text).strip()
    if not text and not args.file:
        raise CommandError("nothing to send: give some text or --file")
    path: Optional[Path] = None
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            raise CommandError("file not found: %s" % path)

    try:
        from telegram import Bot  # imported late so the other commands work without the dependency
    except ImportError:
        raise CommandError("python-telegram-bot is not installed for %s" % sys.executable) from None

    async def send() -> None:
        async with Bot(config.telegram_token) as bot:
            if path is None:
                await bot.send_message(chat_id=chat_id, text=text[:4096])
            elif path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and path.stat().st_size < 10 * 1024 * 1024:
                with path.open("rb") as handle:
                    await bot.send_photo(chat_id=chat_id, photo=handle, caption=text[:1024] or None)
            else:
                with path.open("rb") as handle:
                    await bot.send_document(chat_id=chat_id, document=handle, caption=text[:1024] or None)

    try:
        asyncio.run(send())
    except RuntimeError as exc:
        raise CommandError(str(exc)) from exc
    except Exception as exc:
        raise CommandError("Telegram refused: %s" % exc) from exc
    return {"ok": True, "sent": "file" if path else "message"}


def cmd_screenshot(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from datetime import datetime

    from . import orca

    default = config.home / "outbox" / ("screenshot-%s.png" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    target = Path(args.out).expanduser() if args.out else default
    try:
        saved = orca.screenshot(config, target, page=args.page, full_page=args.full)
    except orca.OrcaError as exc:
        raise CommandError(str(exc)) from exc
    return {"ok": True, "path": str(saved)}


def chrome_command(config: Config) -> List[str]:
    """How to open a window of Alfred's Chrome profile, starting Chrome if it is not running."""
    if not config.chrome_profile:
        raise CommandError("no Chrome profile is set; put ALFRED_CHROME_PROFILE=<profile folder> in .env")
    flag = "--profile-directory=%s" % config.chrome_profile
    if sys.platform == "darwin":
        # -n so the arguments reach a Chrome that is already running, which then opens the profile's window.
        return ["open", "-na", config.chrome_app, "--args", flag]
    return [config.chrome_app, flag]


def cmd_chrome(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    command = chrome_command(config)
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except OSError as exc:
        raise CommandError("cannot open %s: %s" % (config.chrome_app, exc)) from exc
    return {"ok": True, "profile": config.chrome_profile, "note": "give the Claude extension a few seconds to connect"}


def cmd_skills(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    skills = home.list_skills(config)
    return {
        "ok": True,
        "skills": [
            {"name": skill.name, "source": skill.source, "description": skill.description, "path": str(skill.path)}
            for skill in skills
        ],
    }


def _extension_call(action):
    from . import extensions

    try:
        return action(extensions)
    except extensions.ExtensionError as exc:
        raise CommandError(str(exc)) from exc


def cmd_ext_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(
        lambda ext: {"ok": True, "extensions": [item.to_json() for item in ext.list_extensions(config)]}
    )


def cmd_ext_new(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    def action(ext):
        created = ext.scaffold(config, args.name, " ".join(args.description))
        return {
            "ok": True,
            "extension": created.to_json(),
            "next": "add skills/<skill>/SKILL.md and bin/<command>, then run `alfred ext check %s`" % created.name,
        }

    return _extension_call(action)


def cmd_ext_check(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    report = _extension_call(lambda ext: ext.check(config, args.name, run_tests=not args.no_tests))
    if not report["ok"]:
        report["error"] = "; ".join(report["errors"])
    return report


def cmd_ext_enable(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(lambda ext: {"ok": True, "extension": ext.set_enabled(config, args.name, True).to_json()})


def cmd_ext_disable(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(lambda ext: {"ok": True, "extension": ext.set_enabled(config, args.name, False).to_json()})


def cmd_ext_remove(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(lambda ext: {"ok": True, "moved_to": str(ext.remove(config, args.name))})


# -- operator commands --------------------------------------------------------


def cmd_init(config: Config, args: argparse.Namespace) -> int:
    imported = home.bootstrap(config)
    print("Alfred's home is ready at %s" % config.home)
    if imported:
        print("Imported from the old layout: %s" % ", ".join(imported))
    return 0


def cmd_browser_setup(config: Config, args: argparse.Namespace) -> int:
    """Link Alfred's home to Orca so its built-in browser can be used, and prove a page loads."""
    from . import orca

    home.bootstrap(config)
    steps = orca.setup_browser(config, test=not args.no_test)
    for label, ok, detail in steps:
        print("%s %-13s %s" % ("ok  " if ok else "FAIL", label, detail))
    return 0 if all(ok for _, ok, _ in steps) else 1


def cmd_usage(config: Config, args: argparse.Namespace) -> int:
    from . import usage

    print(asyncio.run(usage.fetch(config, dict(os.environ))))
    return 0


def cmd_service(config: Config, args: argparse.Namespace) -> int:
    """Run the bot as a background service that starts at login and restarts if it dies."""
    from . import service

    try:
        if args.action == "install":
            home.bootstrap(config)
            path, notes = service.install(config)
            print("Installed and started. Unit: %s" % path)
            print("Log: %s" % service.log_path(config))
            for note in notes:
                print(note)
        elif args.action == "uninstall":
            print("Stopped and removed." if service.uninstall() else "Nothing was installed.")
        elif args.action == "restart":
            service.restart()
            print("Restarted.")
        elif args.action == "logs":
            print(service.tail_log(config, args.lines))
        else:
            print("Service: %s" % service.status())
            print("Log: %s" % service.log_path(config))
    except service.ServiceError as exc:
        print("alfred: %s" % exc, file=sys.stderr)
        return 1
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    from .telegram_bot import run

    run(config)
    return 0


def cmd_chat(config: Config, args: argparse.Namespace) -> int:
    """Talk to the agent from the terminal. Same home, memory and skills; no Telegram involved."""
    from .agent import Agent

    home.bootstrap(config)
    agent = Agent(config)
    chat_id = args.chat_id if args.chat_id is not None else LOCAL_CHAT_ID
    if args.backend:
        agent.set_backend(chat_id, args.backend)
    if args.new:
        agent.reset(chat_id)

    async def progress(note: str) -> None:
        print("  … %s" % note, file=sys.stderr, flush=True)

    async def turn(text: str) -> bool:
        result = await agent.ask(chat_id, text, username=os.environ.get("USER"), source="terminal", on_progress=progress)
        print(result.text)
        if result.session_restarted:
            print("[earlier conversation could not be resumed; started a fresh one]")
        if result.memory_changes:
            print("[memory: %s]" % ", ".join(result.memory_changes))
        if result.new_skills:
            print("[new skill: %s]" % ", ".join(result.new_skills))
        for change in result.extension_changes:
            print("[extension %s]" % change)
        return not result.is_error

    async def repl() -> bool:
        print("Chatting with Alfred (%s). Ctrl-D to leave." % agent.backend_name(chat_id))
        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, input, "> ")
            except EOFError:
                print()
                return True
            if line.strip():
                await turn(line)

    ok = asyncio.run(turn(args.message) if args.message else repl())
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alfred", description="Alfred: a Telegram agent driven by Claude Code.")
    parser.add_argument("--version", action="version", version="alfred %s" % __version__)
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("run", help="start the Telegram bot and the scheduler").set_defaults(run=cmd_run)
    commands.add_parser("init", help="create or refresh the home directory").set_defaults(run=cmd_init)

    service = commands.add_parser("service", help="keep the bot running in the background")
    service.add_argument("action", choices=("install", "uninstall", "status", "restart", "logs"))
    service.add_argument("-n", "--lines", type=int, default=40, help="log lines to show")
    service.set_defaults(run=cmd_service)

    commands.add_parser("usage", help="show how much of the Claude plan limits is used").set_defaults(run=cmd_usage)

    browser = commands.add_parser("browser-setup", help="link the home to Orca's built-in browser and test it")
    browser.add_argument("--no-test", action="store_true", help="skip opening a test page")
    browser.set_defaults(run=cmd_browser_setup)

    chat = commands.add_parser("chat", help="talk to the agent from this terminal")
    chat.add_argument("-m", "--message", help="send one message and exit")
    chat.add_argument("--chat-id", type=int)
    chat.add_argument("--backend", choices=("claude", "codex"))
    chat.add_argument("--new", action="store_true", help="start a fresh conversation")
    chat.set_defaults(run=cmd_chat)

    commands.add_parser("skills", help="list the agent's skills").set_defaults(json=cmd_skills)

    send = commands.add_parser("send", help="send a message or file to a chat now")
    send.add_argument("--chat-id", type=int)
    send.add_argument("--file")
    send.add_argument("text", nargs="*")
    send.set_defaults(json=cmd_send)

    event = commands.add_parser("event", help="wake the agent now with a note about something that happened")
    event.add_argument("--chat-id", type=int)
    event.add_argument("text", nargs="*")
    event.set_defaults(json=cmd_event)

    ext = commands.add_parser("ext", help="Alfred's own extensions (skills, commands, hooks, MCP servers)")
    ext_actions = ext.add_subparsers(dest="action", required=True)
    ext_actions.add_parser("list", help="every extension, on or off").set_defaults(json=cmd_ext_list)
    ext_new = ext_actions.add_parser("new", help="create an empty extension")
    ext_new.add_argument("name")
    ext_new.add_argument("description", nargs="+")
    ext_new.set_defaults(json=cmd_ext_new)
    ext_check = ext_actions.add_parser("check", help="validate an extension and run its tests")
    ext_check.add_argument("name")
    ext_check.add_argument("--no-tests", action="store_true")
    ext_check.set_defaults(json=cmd_ext_check)
    for verb, handler in (("enable", cmd_ext_enable), ("disable", cmd_ext_disable), ("remove", cmd_ext_remove)):
        sub_parser = ext_actions.add_parser(verb)
        sub_parser.add_argument("name")
        sub_parser.set_defaults(json=handler)

    commands.add_parser("chrome", help="open a window of Alfred's own Chrome profile").set_defaults(json=cmd_chrome)

    shot = commands.add_parser("screenshot", help="save a screenshot of the browser tab to a file")
    shot.add_argument("--page", help="browser page id; defaults to the active tab")
    shot.add_argument("--full", action="store_true", help="capture the whole page, not just the viewport")
    shot.add_argument("--out", help="where to save it; defaults to outbox/")
    shot.set_defaults(json=cmd_screenshot)

    schedule = commands.add_parser("schedule", help="reminders and timed tasks")
    actions = schedule.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("--chat-id", type=int)
    add.add_argument("--at", help="ISO-8601 time; local time if no offset is given")
    add.add_argument("--in", dest="in_", metavar="DURATION", help="relative time such as 20m or 2h")
    add.add_argument("--every", metavar="DURATION", help="repeat interval such as 1d")
    add.add_argument("--message", help="text to deliver as-is")
    add.add_argument("--task", help="prompt for the agent to carry out at that time")
    add.set_defaults(json=cmd_schedule_add)
    listing = actions.add_parser("list")
    listing.add_argument("--chat-id", type=int)
    listing.add_argument("--all", action="store_true")
    listing.set_defaults(json=cmd_schedule_list)
    cancel = actions.add_parser("cancel")
    cancel.add_argument("--chat-id", type=int)
    cancel.add_argument("id")
    cancel.set_defaults(json=cmd_schedule_cancel)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load()
    if getattr(args, "json", None):
        try:
            return emit(args.json(config, args))
        except CommandError as exc:
            return emit({"ok": False, "error": str(exc)})
    if getattr(args, "run", None):
        try:
            return args.run(config, args)
        except RuntimeError as exc:
            print("alfred: %s" % exc, file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
