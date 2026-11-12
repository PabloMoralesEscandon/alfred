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
        raise CommandError("no chat: pass --chat-id or run inside an agent's turn (ALFRED_CHAT_ID)") from None


def resolve_bot_id(config: Config, args: argparse.Namespace) -> int:
    """Which bot: `--bot <@username|id>`, else the one whose turn this is (ALFRED_BOT_ID), else the main bot (0)."""
    wanted = getattr(args, "bot", None)
    if wanted:
        if wanted.strip().lstrip("@").lower() in ("0", "main", "alfred"):
            return 0
        from .bots import BotRegistry

        record = BotRegistry(config).find(wanted)
        if record is None:
            raise CommandError("no bot called %r; run `alfred bots list`" % wanted)
        return record.bot_id
    value = os.environ.get("ALFRED_BOT_ID", "").strip()
    try:
        return int(value) if value else 0
    except ValueError:
        raise CommandError("ALFRED_BOT_ID is not a bot id: %r" % value) from None


def resolve_thread_id(args: argparse.Namespace) -> Optional[int]:
    """The topic to post in: `--thread-id`, or the current turn's topic when the chat also comes from the turn."""
    if getattr(args, "thread_id", None):
        return int(args.thread_id)
    if getattr(args, "chat_id", None) is not None:
        return None  # another chat was named: its topic is not this turn's
    value = os.environ.get("ALFRED_THREAD_ID", "").strip()
    return int(value) if value.isdigit() and int(value) else None


def calling_bot(config: Config):
    """The bot whose agent runs this command (ALFRED_BOT_ID), or None for the operator or the main home."""
    value = os.environ.get("ALFRED_BOT_ID", "").strip()
    if not value or value == "0":
        return None
    from .bots import BotRegistry

    record = BotRegistry(config).get(int(value)) if value.isdigit() else None
    if record is None:
        raise CommandError("this command runs for bot %s, which no longer exists" % value)
    return record


def require(record, permission: str) -> None:
    """A bot without the permission is refused. Claude Code's rules usually stop it first; this is the second lock."""
    if record is not None and not record.allows(permission):
        raise CommandError(
            "%s does not have the %s permission; the user can grant it from the Consiglieri control panel "
            "(/bot perm %s %s on)" % (record.handle, permission, record.username, permission)
        )


def own_bot_only(config: Config, record, args: argparse.Namespace) -> int:
    """A bot acts as itself: it can't post, schedule or wake as another bot."""
    bot_id = resolve_bot_id(config, args)
    if record is not None and bot_id != record.bot_id:
        raise CommandError("%s can only act as itself" % record.handle)
    return bot_id


def own_chat_only(record, chat_id: int) -> None:
    if record is not None and not record.allows("contact") and int(chat_id) not in record.own_chats():
        raise CommandError("%s can only write to its owner's chats; that needs the contact permission" % record.handle)


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
    record = calling_bot(config)
    require(record, "schedule")
    bot_id = own_bot_only(config, record, args)
    chat_id = resolve_chat_id(args)
    own_chat_only(record, chat_id)
    try:
        item = SchedulerStore(config.db_path).add(
            chat_id=chat_id,
            due=due,
            text=args.task or args.message,
            kind=KIND_TASK if args.task else KIND_MESSAGE,
            repeat_seconds=repeat,
            user_id=int(os.environ.get("ALFRED_USER_ID") or 0),
            username=os.environ.get("ALFRED_USERNAME", ""),
            bot_id=bot_id,
            thread_id=resolve_thread_id(args),
        )
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    return {"ok": True, "item": describe(item)}


def cmd_schedule_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    record = calling_bot(config)
    require(record, "schedule")
    items = SchedulerStore(config.db_path).list(
        resolve_chat_id(args), include_done=args.all, bot_id=own_bot_only(config, record, args)
    )
    return {"ok": True, "items": [describe(item) for item in items]}


def cmd_schedule_cancel(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    record = calling_bot(config)
    require(record, "schedule")
    item = SchedulerStore(config.db_path).cancel(args.id, resolve_chat_id(args), bot_id=own_bot_only(config, record, args))
    if item is None:
        raise CommandError("no single pending item matches %r; run `alfred schedule list`" % args.id)
    return {"ok": True, "item": describe(item)}


def cmd_event(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Wake the chat's agent with a note. Works from any process on this machine, not only inside a turn."""
    text = " ".join(args.text).strip()
    if not text:
        raise CommandError("nothing to report: give the text of the event")
    record = calling_bot(config)
    bot_id = own_bot_only(config, record, args)
    chat_id = resolve_chat_id(args)
    own_chat_only(record, chat_id)
    item = SchedulerStore(config.db_path).add(
        chat_id=chat_id,
        due=utc_now(),
        text=text,
        kind=KIND_EVENT,
        user_id=int(os.environ.get("ALFRED_USER_ID") or 0),
        username=os.environ.get("ALFRED_USERNAME", ""),
        bot_id=bot_id,
        thread_id=resolve_thread_id(args),
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
    record = calling_bot(config)
    bot_id = own_bot_only(config, record, args)
    own_chat_only(record, chat_id)
    if record is not None and path is not None:
        from .bots import BotRegistry, path_is_allowed

        if not path_is_allowed(BotRegistry(config).config_for(record.bot_id), record, path):
            raise CommandError("%s can only send files from its own directories" % record.handle)

    try:
        from telegram import Bot  # imported late so the other commands work without the dependency
    except ImportError:
        raise CommandError("python-telegram-bot is not installed for %s" % sys.executable) from None

    token = bot_token(config, bot_id)
    thread_id = resolve_thread_id(args)
    where: Dict[str, Any] = {"chat_id": chat_id}
    if thread_id:
        where["message_thread_id"] = thread_id

    async def send() -> None:
        async with Bot(token) as bot:
            if path is None:
                await bot.send_message(text=text[:4096], **where)
            elif path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and path.stat().st_size < 10 * 1024 * 1024:
                with path.open("rb") as handle:
                    await bot.send_photo(photo=handle, caption=text[:1024] or None, **where)
            else:
                with path.open("rb") as handle:
                    await bot.send_document(document=handle, caption=text[:1024] or None, **where)

    try:
        asyncio.run(send())
    except RuntimeError as exc:
        raise CommandError(str(exc)) from exc
    except Exception as exc:
        raise CommandError("Telegram refused: %s" % exc) from exc
    return {"ok": True, "sent": "file" if path else "message"}


def bot_token(config: Config, bot_id: int) -> str:
    """The token to post as: the main bot's from .env, or a created bot's from its private state folder."""
    if not bot_id:
        try:
            return config.telegram_token
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
    from .bots import BotRegistry

    token = BotRegistry(config).token(bot_id)
    if not token:
        raise CommandError("bot %s has no token; it may have been destroyed" % bot_id)
    return token


def cmd_bots_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .bots import BotRegistry

    require(calling_bot(config), "bots")
    return {
        "ok": True,
        "bots": [
            {"id": record.bot_id, "username": record.username, "name": record.name, "enabled": record.enabled,
             "settings": record.settings, "permissions": {name: record.permission(name) for name in record.permissions},
             "link": record.link}
            for record in BotRegistry(config).list()
        ],
    }


def cmd_bots_set(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Change another bot's setting. Permissions and working directories only change from the control panel."""
    from .bots import BotError, BotRegistry, clean_setting

    caller = calling_bot(config)
    require(caller, "bots")
    registry = BotRegistry(config)
    record = registry.find(args.name)
    if record is None:
        raise CommandError("no bot called %r; run `alfred bots list`" % args.name)
    setting = args.setting.lower()
    if caller is not None:
        if record.bot_id == caller.bot_id:
            raise CommandError("a bot can't change its own settings; the user can, from the Consiglieri control panel")
        if setting == "workdir":
            raise CommandError("only the control panel can change a bot's working directory")
    try:
        value = clean_setting(config, setting, " ".join(args.value))
    except BotError as exc:
        raise CommandError(str(exc)) from exc
    if value:
        record.settings[setting] = value
    else:
        record.settings.pop(setting, None)
    registry.save(record)
    return {"ok": True, "bot": record.username, "settings": record.settings,
            "note": "applies from the bot's next message"}


def cmd_bots_new(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Ask the owner, through the control panel, to create a bot. It starts with the default permissions."""
    from . import bots

    caller = calling_bot(config)
    require(caller, "bots")
    owner = caller.owner_id if caller is not None else int(os.environ.get("ALFRED_USER_ID") or args.owner or 0)
    if not owner:
        raise CommandError("no owner: pass --owner <telegram user id>")
    task = " ".join(args.task).strip()
    settings = {"task": task} if task else {}

    try:
        from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        raise CommandError("python-telegram-bot is not installed for %s" % sys.executable) from None

    async def ask() -> str:
        async with Bot(bot_token(config, 0)) as panel:
            registry = bots.BotRegistry(config)
            username = bots.suggest_username(args.name, panel.username, task, registry.known_usernames())
            registry.add_pending(owner, owner, args.name, username, settings)
            link = bots.creation_link(panel.username, username, args.name)
            asked_by = " (%s asks for it)" % caller.handle if caller is not None else ""
            await panel.send_message(
                chat_id=owner,
                text="Create a bot called %s%s? Tap the button and confirm in Telegram.%s" % (
                    args.name, asked_by, "\nTask: %s" % task if task else ""),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Create @%s" % username, url=link)]]),
            )
            return link

    try:
        link = asyncio.run(ask())
    except Exception as exc:
        raise CommandError("Telegram refused: %s" % exc) from exc
    return {"ok": True, "link": link, "note": "the owner has to confirm it in Telegram; the control panel takes it from there"}


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
    """How to open a window of Consiglieri's Chrome profile, starting Chrome if it is not running."""
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
    print("Consiglieri's home is ready at %s" % config.home)
    if imported:
        print("Imported from the old layout: %s" % ", ".join(imported))
    return 0


def cmd_browser_setup(config: Config, args: argparse.Namespace) -> int:
    """Link Consiglieri's home to Orca so its built-in browser can be used, and prove a page loads."""
    from . import orca

    home.bootstrap(config)
    steps = orca.setup_browser(config, test=not args.no_test)
    for label, ok, detail in steps:
        print("%s %-13s %s" % ("ok  " if ok else "FAIL", label, detail))
    return 0 if all(ok for _, ok, _ in steps) else 1


def require_usage(config: Config, record) -> None:
    """A bot reads usage and sets usage alerts only with the usage permission and with the monitor switched on."""
    if record is None:
        return  # the operator at a terminal
    require(record, "usage")
    from . import usage_monitor

    if not usage_monitor.is_enabled(config):
        raise CommandError(
            "the usage monitor is off (%s), so the usage permission has no effect yet; ask the user to switch "
            "it on" % usage_monitor.ENABLE_SETTING
        )


def usage_alert_bot(config: Config, args: argparse.Namespace, adding: bool = False) -> int:
    """The bot whose usage alerts these are: the calling bot itself, or `--bot` for the operator."""
    record = calling_bot(config)
    if record is not None:
        require(record, "usage")
    bot_id = own_bot_only(config, record, args)
    if not bot_id:
        raise CommandError("usage alerts belong to the bots Consiglieri created; pass --bot <@username>")
    if adding:
        from .bots import BotRegistry
        from . import usage_monitor

        target = record or BotRegistry(config).get(bot_id)
        if target is None or not target.allows("usage"):
            raise CommandError("that bot does not have the usage permission (/bot perm <bot> usage on)")
        if not usage_monitor.is_enabled(config):
            raise CommandError(
                "the usage monitor is off (%s), so an alert would never be checked; ask the user to switch it on"
                % usage_monitor.ENABLE_SETTING
            )
    return bot_id


def cmd_usage_alert_add(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from . import usage_alerts, usage_monitor

    record = calling_bot(config)
    bot_id = usage_alert_bot(config, args, adding=True)
    chat_id = resolve_chat_id(args)
    own_chat_only(record, chat_id)
    try:
        alert = usage_alerts.AlertStore.for_bot(config, bot_id).add(
            args.when,
            note=" ".join(args.note).strip(),
            repeat=args.repeat,
            chat_id=chat_id,
            thread_id=resolve_thread_id(args),
            user_id=int(os.environ.get("ALFRED_USER_ID") or 0),
            username=os.environ.get("ALFRED_USERNAME", ""),
        )
    except usage_alerts.ConditionError as exc:
        raise CommandError(str(exc)) from exc
    settings = usage_monitor.MonitorSettings.from_config(config)
    return {
        "ok": True,
        "alert": alert.describe(),
        "checked_every_minutes": settings.poll_seconds // 60,
        "cost": "checking is free; each wake-up is one turn of yours and uses plan usage, "
                "at most one every %d minutes" % (settings.wake_interval_seconds // 60),
    }


def cmd_usage_alert_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from . import usage_alerts, usage_monitor

    bot_id = usage_alert_bot(config, args)
    alerts = usage_alerts.AlertStore.for_bot(config, bot_id).alerts()
    return {"ok": True, "monitor_on": usage_monitor.is_enabled(config), "alerts": [alert.describe() for alert in alerts]}


def cmd_usage_alert_remove(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from . import usage_alerts

    bot_id = usage_alert_bot(config, args)
    alert = usage_alerts.AlertStore.for_bot(config, bot_id).remove(args.id)
    if alert is None:
        raise CommandError("no single alert matches %r; run `alfred usage-alert list`" % args.id)
    return {"ok": True, "removed": alert.describe()}


def cmd_usage(config: Config, args: argparse.Namespace) -> int:
    from . import usage

    try:
        require_usage(config, calling_bot(config))
    except CommandError as exc:
        print("alfred: %s" % exc, file=sys.stderr)
        return 1
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
    bot_name = "Alfred"
    if args.bot:
        from .bots import BotRegistry, prepare_workspace

        record = BotRegistry(config).find(args.bot)
        if record is None:
            print("alfred: no bot called %r" % args.bot, file=sys.stderr)
            return 1
        config = BotRegistry(config).config_for(record.bot_id)
        prepare_workspace(config)
        bot_name = record.handle
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
        print("Chatting with %s (%s). Ctrl-D to leave." % (bot_name, agent.backend_name(chat_id)))
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
    parser = argparse.ArgumentParser(prog="alfred", description="Consiglieri: a set of Telegram agents driven by Claude Code.")
    parser.add_argument("--version", action="version", version="alfred %s" % __version__)
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("run", help="start the Telegram bot and the scheduler").set_defaults(run=cmd_run)
    commands.add_parser("init", help="create or refresh the home directory").set_defaults(run=cmd_init)

    service = commands.add_parser("service", help="keep the bot running in the background")
    service.add_argument("action", choices=("install", "uninstall", "status", "restart", "logs"))
    service.add_argument("-n", "--lines", type=int, default=40, help="log lines to show")
    service.set_defaults(run=cmd_service)

    commands.add_parser("usage", help="show how much of the Claude plan limits is used").set_defaults(run=cmd_usage)

    alert = commands.add_parser(
        "usage-alert", help="wake this bot when a condition on the Claude plan usage holds",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Values: session.used, week.used (percent used), session.left, week.left (time to the reset).\n"
                    "Operators: < <= > >= = !=, joined with and, or, not and parentheses.\n"
                    "Example: alfred usage-alert add --when \"session.left < 1h and session.used = 0\" use the spare window\n"
                    "An alert fires once (or with --repeat, once per window). Each wake-up uses plan usage.",
    )
    alert_actions = alert.add_subparsers(dest="action", required=True)
    alert_add = alert_actions.add_parser("add", help="add an alert; the rest of the line is a note you get back")
    alert_add.add_argument("--when", required=True, help='for example "week.left < 1d and week.used < 60"')
    alert_add.add_argument("--repeat", action="store_true", help="fire again in every new window, not just once")
    alert_add.add_argument("--chat-id", type=int)
    alert_add.add_argument("--thread-id", type=int)
    alert_add.add_argument("--bot", help=argparse.SUPPRESS)
    alert_add.add_argument("note", nargs="*")
    alert_add.set_defaults(json=cmd_usage_alert_add)
    alert_list = alert_actions.add_parser("list", help="this bot's alerts")
    alert_list.add_argument("--bot", help=argparse.SUPPRESS)
    alert_list.set_defaults(json=cmd_usage_alert_list)
    alert_remove = alert_actions.add_parser("remove", help="delete an alert by id or id prefix")
    alert_remove.add_argument("--bot", help=argparse.SUPPRESS)
    alert_remove.add_argument("id")
    alert_remove.set_defaults(json=cmd_usage_alert_remove)

    browser = commands.add_parser("browser-setup", help="link the home to Orca's built-in browser and test it")
    browser.add_argument("--no-test", action="store_true", help="skip opening a test page")
    browser.set_defaults(run=cmd_browser_setup)

    chat = commands.add_parser("chat", help="talk to the agent from this terminal")
    chat.add_argument("-m", "--message", help="send one message and exit")
    chat.add_argument("--chat-id", type=int)
    chat.add_argument("--bot", help="talk to one of the bots Consiglieri created, by @username, name or id")
    chat.add_argument("--backend", choices=("claude", "codex"))
    chat.add_argument("--new", action="store_true", help="start a fresh conversation")
    chat.set_defaults(run=cmd_chat)

    commands.add_parser("skills", help="list the agent's skills").set_defaults(json=cmd_skills)

    send = commands.add_parser("send", help="send a message or file to a chat now")
    send.add_argument("--chat-id", type=int)
    send.add_argument("--bot", help="which bot posts it: @username or id; default the one whose turn this is")
    send.add_argument("--thread-id", type=int, help="the topic to post in")
    send.add_argument("--file")
    send.add_argument("text", nargs="*")
    send.set_defaults(json=cmd_send)

    event = commands.add_parser("event", help="wake the agent now with a note about something that happened")
    event.add_argument("--chat-id", type=int)
    event.add_argument("--bot", help="which bot's agent to wake: @username or id; default the main one")
    event.add_argument("--thread-id", type=int)
    event.add_argument("text", nargs="*")
    event.set_defaults(json=cmd_event)

    ext = commands.add_parser("ext", help="an agent's own extensions (skills, commands, hooks, MCP servers)")
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

    bots_parser = commands.add_parser("bots", help="the Telegram bots Consiglieri created")
    bots_actions = bots_parser.add_subparsers(dest="action", required=True)
    bots_actions.add_parser("list", help="every bot and its settings").set_defaults(json=cmd_bots_list)
    bots_set = bots_actions.add_parser("set", help="change one setting of a bot")
    bots_set.add_argument("name", help="@username, name or id")
    bots_set.add_argument("setting", help="task, model, effort, backend or workdir")
    bots_set.add_argument("value", nargs="+", help="the new value, or `default` to clear it")
    bots_set.set_defaults(json=cmd_bots_set)
    bots_new = bots_actions.add_parser("new", help="ask the owner to create a bot (they confirm in Telegram)")
    bots_new.add_argument("name")
    bots_new.add_argument("task", nargs="*")
    bots_new.add_argument("--owner", type=int, help="Telegram user id to ask, outside a bot's turn")
    bots_new.set_defaults(json=cmd_bots_new)

    commands.add_parser("chrome", help="open a window of Consiglieri's own Chrome profile").set_defaults(json=cmd_chrome)

    shot = commands.add_parser("screenshot", help="save a screenshot of the browser tab to a file")
    shot.add_argument("--page", help="browser page id; defaults to the active tab")
    shot.add_argument("--full", action="store_true", help="capture the whole page, not just the viewport")
    shot.add_argument("--out", help="where to save it; defaults to outbox/")
    shot.set_defaults(json=cmd_screenshot)

    schedule = commands.add_parser("schedule", help="reminders and timed tasks")
    actions = schedule.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("--chat-id", type=int)
    add.add_argument("--bot", help=argparse.SUPPRESS)
    add.add_argument("--thread-id", type=int, help=argparse.SUPPRESS)
    add.add_argument("--at", help="ISO-8601 time; local time if no offset is given")
    add.add_argument("--in", dest="in_", metavar="DURATION", help="relative time such as 20m or 2h")
    add.add_argument("--every", metavar="DURATION", help="repeat interval such as 1d")
    add.add_argument("--message", help="text to deliver as-is")
    add.add_argument("--task", help="prompt for the agent to carry out at that time")
    add.set_defaults(json=cmd_schedule_add)
    listing = actions.add_parser("list")
    listing.add_argument("--chat-id", type=int)
    listing.add_argument("--bot", help=argparse.SUPPRESS)
    listing.add_argument("--all", action="store_true")
    listing.set_defaults(json=cmd_schedule_list)
    cancel = actions.add_parser("cancel")
    cancel.add_argument("--chat-id", type=int)
    cancel.add_argument("--bot", help=argparse.SUPPRESS)
    cancel.add_argument("id")
    cancel.set_defaults(json=cmd_schedule_cancel)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load()
    bot_id = os.environ.get("ALFRED_BOT_ID", "").strip()
    if getattr(args, "json", None) and bot_id.isdigit() and int(bot_id):
        # Inside a bot's turn: its skills, extensions and directory, not the main home's.
        from .bots import BotRegistry

        registry = BotRegistry(config)
        if registry.get(int(bot_id)) is not None:
            config = registry.config_for(int(bot_id))
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
