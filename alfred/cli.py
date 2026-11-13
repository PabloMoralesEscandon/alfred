"""The `alfred` command.

For the operator: `run`, `init`, `service`, `chat --bot`. For a bot's agent (through `bin/alfred`
on its PATH): `schedule`, `send`, `event`, `skills`, `ext`. The agent-facing commands print JSON so
the model can read the outcome reliably.

There is no main agent. Whatever an agent does belongs to a bot: inside a bot's turn the bot is
ALFRED_BOT_ID, and from a terminal or a script it is `--bot <@username|id>`. The control panel itself
never posts, schedules or wakes anything through these commands.
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
from .scheduler import KIND_EVENT, KIND_MESSAGE, KIND_TASK, SchedulerStore, parse_duration, parse_time, to_utc_text, utc_now

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


NO_BOT = ("which bot? Inside a bot's turn it is the bot whose turn it is; from a terminal or a script pass "
          "--bot <@username|id>. There is no main agent: the control panel runs none")


def resolve_bot_id(config: Config, args: argparse.Namespace) -> int:
    """Which bot: `--bot <@username|id>`, else the one whose turn this is (ALFRED_BOT_ID), else 0 (none)."""
    wanted = getattr(args, "bot", None)
    if wanted:
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
    """A bot acts as itself: it can't post, schedule or wake as another bot. Some bot it must be."""
    bot_id = resolve_bot_id(config, args)
    if record is not None and bot_id != record.bot_id:
        raise CommandError("%s can only act as itself" % record.handle)
    if not bot_id:
        raise CommandError(NO_BOT)
    return bot_id


def bot_directory_only(config: Config) -> None:
    """Skills and extensions are a bot's own; outside a bot's turn there is none to look at."""
    if not config.bot_id:
        raise CommandError("this works on a bot's own directory, inside that bot's turn; there is no main agent")


def own_chat_only(record, chat_id: int) -> None:
    if record is not None and not record.allows("contact") and int(chat_id) not in record.own_chats():
        raise CommandError("%s can only write to its owner's chats; that needs the contact permission" % record.handle)


def describe(item: Dict[str, Any]) -> Dict[str, Any]:
    due = parse_time(item["due_at_utc"])
    described = {
        "id": item["id"],
        "kind": item["kind"],
        "text": item["message"],
        "status": item["status"],
        "due_utc": item["due_at_utc"],
        "due_local": due.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "repeat_seconds": item["repeat_seconds"],
        "last_error": item["last_error"],
    }
    if item.get("gate"):
        from .usage_gate import Gate, GateError

        try:
            described["usage"] = Gate.from_text(item["gate"]).describe()
        except GateError as exc:
            described["usage"] = {"error": str(exc)}
        described["skips"] = item.get("skips") or 0
        described["last_skip"] = item.get("last_skip")
    return described


def schedule_gate(config: Config, record, args: argparse.Namespace) -> Optional[str]:
    """The usage condition of `schedule add`, stored as text; None without one. Needs the usage permission."""
    from . import usage_gate, usage_monitor

    asked = [name for name in ("skip_if_low", "skip_if", "only_if") if getattr(args, name, None)]
    if args.only_if_waste:
        asked.append("only_if_waste")
    if not asked:
        for flag, value in (("--threshold", args.threshold), ("--margin", args.margin), ("--if-unread", args.if_unread),
                            ("--hours", args.hours), ("--days", args.days), ("--tz", args.tz)):
            if value is not None and value is not False:
                raise CommandError("%s goes with a usage condition: --skip-if-low, --skip-if, --only-if or "
                                   "--only-if-waste" % flag)
        if args.projected:
            raise CommandError("--projected goes with --skip-if-low")
        return None
    if record is not None:
        require(record, "usage")
    if args.skip_if_low and args.skip_if:
        raise CommandError("give one of --skip-if-low or --skip-if (--skip-if can say both: 'a or b')")
    if args.only_if_waste and args.only_if:
        raise CommandError("give one of --only-if-waste or --only-if")
    if (args.threshold is not None or args.projected) and not args.skip_if_low:
        raise CommandError("--threshold and --projected go with --skip-if-low")
    if args.margin is not None and not args.only_if_waste:
        raise CommandError("--margin goes with --only-if-waste")
    labels = []
    skip_if = args.skip_if or ""
    if args.skip_if_low:
        threshold = args.threshold
        if threshold is None:
            threshold = usage_monitor.MonitorSettings.from_config(config).low_threshold
        if not 0 < threshold <= 100:
            raise CommandError("the threshold is a percentage between 1 and 100")
        skip_if = running_low_condition(args.skip_if_low, threshold, args.projected)
        labels.append("--skip-if-low %s" % args.skip_if_low)
    only_if = args.only_if or ""
    if args.only_if_waste:
        only_if = WASTE_CONDITION % (args.margin or 0)
        labels.append("--only-if-waste")
    gate = usage_gate.Gate(skip_if=skip_if, only_if=only_if, hours=_hours_spec(args), if_unread=args.if_unread or "",
                           label=" ".join(labels))
    try:
        return gate.validate().to_text()
    except usage_gate.GateError as exc:
        raise CommandError(str(exc)) from None


def gate_note(text: str) -> str:
    from .usage_gate import Gate

    gate = Gate.from_text(text)
    parts = []
    if gate.skip_if:
        parts.append("not delivered when %s" % gate.skip_if)
    if gate.only_if:
        parts.append("delivered only when %s" % gate.only_if)
    return ("Checked just before each delivery: %s. A held-back delivery does not wake you and costs no usage; a "
            "recurring item moves on to its next time, a one-off ends as skipped. If the usage can't be read, it is "
            "%s." % ("; ".join(parts), "delivered" if gate.unread_policy == "run" else "skipped"))


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
    gate = schedule_gate(config, record, args)
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
            gate=gate,
        )
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    payload = {"ok": True, "item": describe(item)}
    if gate:
        payload["usage_note"] = gate_note(gate)
    return payload


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
    """The token to post as: a bot's, from its private state folder. Bot 0 is the panel's, from .env, used only to
    ask the owner to create a bot (`alfred bots new`)."""
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


def verified_caller(config: Config):
    """The bot calling `alfred bots`, checked against the key its agent was started with (ALFRED_BOT_KEY): a bot
    that changes ALFRED_BOT_ID to another bot's does not know that bot's key. None outside a bot's turn."""
    from .bots import key_matches

    record = calling_bot(config)
    if record is not None and not key_matches(config, record.bot_id, os.environ.get("ALFRED_BOT_KEY", "")):
        raise CommandError("can't confirm that this runs for %s (ALFRED_BOT_KEY does not match); run it from that "
                           "bot's own turn" % record.handle)
    return record


def admin_caller(config: Config):
    """The bot admin whose turn this is. The helpers never run outside one: there is no admin to check against."""
    caller = verified_caller(config)
    if caller is None:
        raise CommandError("the bot admin helpers run only inside a bot admin's turn; from a terminal, use the control "
                           "panel's /bot menu")
    require(caller, "admin")
    return caller


def _caller_level_covers(config: Config, caller, record) -> List[str]:
    from .bot_admin import ceiling_problems

    return ceiling_problems(config, caller, record.permissions, record.cross_agent, record.settings)


def cmd_bots_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from . import presets
    from .bots import BotRegistry

    caller = verified_caller(config)
    if caller is not None and not caller.allows("admin"):
        require(caller, "bots")  # either permission lists the owner's bots
    owner = caller.owner_id if caller is not None else None  # a bot sees only its owner's bots
    return {
        "ok": True,
        "bots": [
            {"id": record.bot_id, "username": record.username, "name": record.name, "enabled": record.enabled,
             "settings": record.settings, "permissions": {name: record.permission(name) for name in record.permissions},
             "preset": presets.level_name(record), "link": record.link,
             "created_by_you": caller is not None and record.created_by == caller.bot_id,
             "started": int(record.owner_id) in record.chat_ids}
            for record in BotRegistry(config).list(owner_id=owner)
        ],
    }


def cmd_bots_set(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Change another bot's setting. Permissions and working directories only change from the control panel."""
    from .bots import BotError, BotRegistry, clean_setting

    caller = verified_caller(config)
    require(caller, "bots")
    registry = BotRegistry(config)
    record = registry.find(args.name, owner_id=caller.owner_id if caller is not None else None)
    if record is None:  # another user's bot reads as missing too
        raise CommandError("no bot called %r; run `alfred bots list`" % args.name)
    setting = args.setting.lower()
    if caller is not None:
        if record.bot_id == caller.bot_id:
            raise CommandError("a bot can't change its own settings; the user can, from the Consiglieri control panel")
        if setting == "workdir":
            raise CommandError("only the control panel can change a bot's working directory")
        if setting == "backend" and " ".join(args.value).strip().lower() == "codex" and \
                (caller.settings.get("backend") or config.default_backend) != "codex":
            raise CommandError("a bot can't move another to codex, which enforces fewer rules than claude")
        above = _caller_level_covers(config, caller, record)
        if above:  # its task and model steer it: a bot doesn't steer one that may do more than itself
            raise CommandError("%s may do more than %s (%s); only the user changes it, from the control panel"
                               % (record.handle, caller.handle, "; ".join(above)))
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

    caller = verified_caller(config)
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


SPEC_SIZE_LIMIT = 64 * 1024


def read_spec(config: Config, caller, args: argparse.Namespace) -> Any:
    """The JSON a bot admin passes: --json '<object>' or --file <path in its own directories>."""
    from .bots import path_is_allowed

    if bool(getattr(args, "json_text", None)) == bool(getattr(args, "file", None)):
        raise CommandError("give the spec with exactly one of --file <path> or --json '<object>'")
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_absolute():
            path = config.workspace / path
        if not path_is_allowed(config, caller, path):
            raise CommandError("the spec file must be in your own directories")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError("can't read %s: %s" % (path, exc)) from exc
    else:
        text = args.json_text
    if len(text) > SPEC_SIZE_LIMIT:
        raise CommandError("the spec is too long (at most %d bytes)" % SPEC_SIZE_LIMIT)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CommandError("the spec is not valid JSON: %s" % exc) from exc


def _refuse(config: Config, caller, problems: List[str], data: Any) -> None:
    """A refused spec or request goes to the audit log, with what it asked for, and back to the bot."""
    from . import bots

    bots.audit(config, "admin_refused", caller, caller.handle, "alfred bots", admin=caller.handle,
               problems=problems, input=json.dumps(data, ensure_ascii=False, default=str)[:4000])
    raise CommandError("refused: %s" % "; ".join(problems))


def cmd_bots_spec(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Check a spec without sending anything: what the bot would start with, and what would wait for the user."""
    from . import bot_admin, presets

    caller = admin_caller(config)
    data = read_spec(config, caller, args)
    try:
        checked = bot_admin.parse_spec(config, caller, data)
    except bot_admin.SpecError as exc:
        return {"ok": False, "error": "this spec would be refused", "problems": exc.problems}
    limits = bot_admin.limit_problems(config, caller)
    return {
        "ok": True,
        "spec": checked.as_dict(),
        "starts_with": presets.name_of(checked.initial),
        "waits_for_the_users_yes": [bot_admin.EXTRAS[key][0] for key in checked.extras],
        "limits": limits or "within the limits",
        "next": "alfred bots create with the same spec sends it to the user" if not limits else "a limit stops it now",
    }


def cmd_bots_create(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Send the user a request to create a bot from a spec. The user confirms it in Telegram and owns it."""
    from . import bot_admin, bots

    caller = admin_caller(config)
    data = read_spec(config, caller, args)
    try:
        checked = bot_admin.parse_spec(config, caller, data)
    except bot_admin.SpecError as exc:
        _refuse(config, caller, exc.problems, data)
    limits = bot_admin.limit_problems(config, caller)
    if limits:
        _refuse(config, caller, limits, data)
    chat_id = resolve_chat_id(args)
    own_chat_only(caller, chat_id)
    owner = int(caller.owner_id)  # always the admin's owner, whatever the environment says
    try:
        from telegram import Bot
    except ImportError:
        raise CommandError("python-telegram-bot is not installed for %s" % sys.executable) from None
    registry = bots.BotRegistry(config)

    async def ask() -> Dict[str, Any]:
        async with Bot(bot_token(config, 0)) as panel:
            taken = registry.known_usernames()
            username = checked.username
            if not username or username.lower() in {name.lower() for name in taken + [panel.username]}:
                username = bots.suggest_username(checked.name, panel.username, checked.settings.get("task", ""), taken)
            request = bot_admin.new_request(caller, checked, data, username, chat_id, resolve_thread_id(args))
            link = bots.creation_link(panel.username, username, checked.name)
            request["link"] = link
            registry.add_pending(owner, owner, checked.name, username, checked.settings, checked.initial, checked.soul,
                                 request_id=request["id"], admin_bot_id=caller.bot_id)
            bot_admin.add(config, request)
            intro = bot_admin.summary(config, caller, checked, username, request["id"]) + (
                "\n\nTo create it, tap \"Create @%s\" on the keyboard below and confirm in Telegram's window; or "
                "Cancel. If the keyboard doesn't show, use the link in the next message." % username)
            try:
                for text, markup in bot_admin.creation_messages(intro, checked.name, username, link):
                    await panel.send_message(chat_id=owner, text=text, reply_markup=markup)
            except Exception:
                registry.take_pending(owner, username)
                bot_admin.update(config, request["id"], status="cancelled", problems=["Telegram refused the message"])
                raise
            return dict(request, panel=panel.username)

    try:
        request = asyncio.run(ask())
    except Exception as exc:
        raise CommandError("Telegram refused: %s" % exc) from exc
    bots.audit(config, "admin_requested", caller, caller.handle, "alfred bots create", admin=caller.handle,
               request=request["id"], username=request["username"], spec=checked.as_dict(),
               extras=[bot_admin.EXTRAS[key][0] for key in checked.extras])
    return {
        "ok": True,
        "request": request["id"],
        "username": request["username"],
        "link": request["link"],
        "waits_for_the_users_yes": [bot_admin.EXTRAS[key][0] for key in checked.extras],
        "next": ("Tell the user the request is in their chat with @%s: they tap \"Create @%s\" and confirm. You are "
                 "woken with an [Event] when the bot exists (or check with alfred bots status %s). The link above "
                 "is the second option." % (request["panel"], request["username"], request["id"])),
    }


def cmd_bots_status(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from . import bot_admin
    from .bots import BotRegistry

    caller = admin_caller(config)
    registry = BotRegistry(config)
    mine = [item for item in bot_admin.load(config) if int(item["admin_bot_id"]) == caller.bot_id]
    if args.request:
        mine = [item for item in mine if item["id"] == args.request]
        if not mine:
            raise CommandError("no request %r of yours" % args.request)
    return {"ok": True, "requests": [bot_admin.public(item, registry) for item in mine[-20:]],
            "limits": bot_admin.limits(config), "limits_now": bot_admin.limit_problems(config, caller) or "within"}


def cmd_bots_configure(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Finish setting up a bot this admin asked for: task, model, effort, personality, reminders. Never permissions."""
    from . import bot_admin, bots, soul

    caller = admin_caller(config)
    registry = bots.BotRegistry(config)
    record = registry.find(args.name, owner_id=caller.owner_id)
    if record is None or record.created_by != caller.bot_id:
        raise CommandError("no bot called %r that you asked for; a bot admin sets up only the bots it requested"
                           % args.name)
    above = _caller_level_covers(config, caller, record)
    if above:
        raise CommandError("%s may now do more than you (%s); only the user changes it, from the control panel"
                           % (record.handle, "; ".join(above)))
    data = read_spec(config, caller, args)
    try:
        changes = bot_admin.parse_changes(config, data)
    except bot_admin.SpecError as exc:
        bots.audit(config, "admin_refused", record, caller.handle, "alfred bots configure", admin=caller.handle,
                   problems=exc.problems)
        raise CommandError("refused: %s" % "; ".join(exc.problems)) from exc
    if changes.get("reminders") and not record.allows("schedule"):
        raise CommandError("%s does not have the schedule permission" % record.handle)
    for key in ("task", "model", "effort"):
        if key in changes:
            if changes[key]:
                record.settings[key] = changes[key]
            else:
                record.settings.pop(key, None)
    registry.save(record)
    if "soul" in changes:
        soul.write(registry.workspace(record.bot_id), changes["soul"])
    scheduled = bot_admin.schedule_reminders(config, record, changes.get("reminders") or [])
    fields = sorted(changes)
    bots.audit(config, "admin_configured", record, caller.handle, "alfred bots configure", admin=caller.handle,
               fields=fields, request=record.request_id)
    return {"ok": True, "bot": record.username, "changed": fields, "reminders_scheduled": len(scheduled),
            "note": "applies from its next message"}


def cmd_screenshot(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from datetime import datetime

    from . import orca

    default = config.workspace / "outbox" / ("screenshot-%s.png" % datetime.now().strftime("%Y%m%d-%H%M%S"))
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
    bot_directory_only(config)
    skills = home.list_skills(config)
    return {
        "ok": True,
        "skills": [
            {"name": skill.name, "source": skill.source, "description": skill.description, "path": str(skill.path)}
            for skill in skills
        ],
    }


def _extension_call(action, config: Config):
    from . import extensions

    bot_directory_only(config)
    try:
        return action(extensions)
    except extensions.ExtensionError as exc:
        raise CommandError(str(exc)) from exc


def cmd_ext_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(
        lambda ext: {"ok": True, "extensions": [item.to_json() for item in ext.list_extensions(config)]}, config
    )


def cmd_ext_new(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    def action(ext):
        created = ext.scaffold(config, args.name, " ".join(args.description))
        return {
            "ok": True,
            "extension": created.to_json(),
            "next": "add skills/<skill>/SKILL.md and bin/<command>, then run `alfred ext check %s`" % created.name,
        }

    return _extension_call(action, config)


def cmd_ext_check(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    report = _extension_call(lambda ext: ext.check(config, args.name, run_tests=not args.no_tests), config)
    if not report["ok"]:
        report["error"] = "; ".join(report["errors"])
    return report


def cmd_ext_enable(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(lambda ext: {"ok": True, "extension": ext.set_enabled(config, args.name, True).to_json()}, config)


def cmd_ext_disable(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(lambda ext: {"ok": True, "extension": ext.set_enabled(config, args.name, False).to_json()}, config)


def cmd_ext_remove(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    return _extension_call(lambda ext: {"ok": True, "moved_to": str(ext.remove(config, args.name))}, config)


# -- operator commands --------------------------------------------------------


def cmd_init(config: Config, args: argparse.Namespace) -> int:
    home.prepare(config)
    print("Consiglieri's home (the control panel's state) is ready at %s" % config.home)
    return 0


def cmd_browser_setup(config: Config, args: argparse.Namespace) -> int:
    """Link Consiglieri's home to Orca so its built-in browser can be used, and prove a page loads."""
    from . import orca

    home.prepare(config)
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


def _hours_spec(args: argparse.Namespace) -> str:
    """The active hours from --hours, --days and --tz, normalized; empty when none was given."""
    from . import usage_forecast

    if not (getattr(args, "hours", None) or getattr(args, "days", None) or getattr(args, "tz", None)):
        return ""
    try:
        return usage_forecast.parse_hours(args.hours, args.days, args.tz).spec()
    except usage_forecast.HoursError as exc:
        raise CommandError(str(exc)) from exc


def _add_usage_alert(config: Config, args: argparse.Namespace, when: str, note: str) -> Dict[str, Any]:
    from . import usage_alerts, usage_monitor

    record = calling_bot(config)
    bot_id = usage_alert_bot(config, args, adding=True)
    chat_id = resolve_chat_id(args)
    own_chat_only(record, chat_id)
    try:
        alert = usage_alerts.AlertStore.for_bot(config, bot_id).add(
            when,
            note=note,
            repeat=args.repeat,
            chat_id=chat_id,
            thread_id=resolve_thread_id(args),
            user_id=int(os.environ.get("ALFRED_USER_ID") or 0),
            username=os.environ.get("ALFRED_USERNAME", ""),
            hours=_hours_spec(args),
        )
    except usage_alerts.ConditionError as exc:
        raise CommandError(str(exc)) from exc
    settings = usage_monitor.MonitorSettings.from_config(config)
    payload = {
        "ok": True,
        "alert": alert.describe(),
        "checked_every_minutes": settings.poll_seconds // 60,
        "cost": "checking is free; each wake-up is one turn of yours and uses plan usage, "
                "at most one every %d minutes" % (settings.wake_interval_seconds // 60),
    }
    if alert.condition.uses_forecast:
        payload["active_hours"] = alert.hours or settings.active_hours.spec()
        payload["fires"] = ("once per quota week, and again that week only if the loss grows by one window's share "
                            "or ALFRED_USAGE_SURPLUS_STEP points; " + ("armed again every week" if alert.repeat
                                                                       else "spent when that week resets"))
    return payload


def cmd_usage_alert_add(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    words = list(args.note)
    when = args.when
    if not when:
        if not words:
            raise CommandError("give a condition, such as: alfred usage-alert add 'week.surplus > 0' --hours 9-20")
        when = words.pop(0)
    return _add_usage_alert(config, args, when, " ".join(words).strip())


WASTE_CONDITION = "week.surplus >= %g and week.used < 100"


def cmd_usage_alert_waste(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """The common case: wake me when weekly quota can no longer be used up before the reset."""
    return _add_usage_alert(config, args, WASTE_CONDITION % args.margin, " ".join(args.note).strip())


def cmd_usage_alert_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from . import usage_alerts, usage_monitor

    bot_id = usage_alert_bot(config, args)
    alerts = usage_alerts.AlertStore.for_bot(config, bot_id).alerts()
    return {"ok": True, "monitor_on": usage_monitor.is_enabled(config),
            "alerts": [alert.describe() for alert in alerts]}


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


# On-demand usage questions: a verdict, never raw numbers to interpret (the breakdown only with --json).
EXIT_ANSWERED = 0  # true or false was answered
EXIT_REFUSED = 1  # no usage permission, or the bot no longer exists
EXIT_BAD_QUESTION = 2  # the condition or an option can't be read
EXIT_UNREAD = 3  # the plan usage could not be read


class UsageQueryError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _span_text(seconds: Optional[float]) -> Optional[str]:
    from .usage_alerts import _span

    return _span(seconds) if seconds is not None else None


def _limits(reading, now) -> Dict[str, Any]:
    """Every meter: percent used and left, and when it resets."""
    limits: Dict[str, Any] = {}
    for window in reading.windows:
        name = {"session": "session", "weekly_all": "week"}.get(window.kind) or (
            "week_%s" % (window.scope or window.kind).lower())
        left = (window.resets_at - now).total_seconds() if window.resets_at else None
        limits[name] = {
            "label": window.label, "used": window.percent, "left": round(max(0.0, 100 - window.percent), 1),
            "resets_at": to_utc_text(window.resets_at) if window.resets_at else None,
            "resets_in": _span_text(max(0.0, left)) if left is not None else None, "severity": window.severity,
        }
    if "session" not in limits:
        limits["session"] = {"label": "Session", "running": False, "used": None, "left": None, "resets_at": None,
                             "resets_in": None}
    return limits


def _usage_question(config: Config, args: argparse.Namespace, condition: str, answer: Any = None) -> int:
    """Evaluate a condition on a cached or new reading and print the verdict: `true: <reason>` or `false: ...`.

    `answer(result, reason, evaluation, forecast)` may return another (result, reason) and extra JSON fields.
    """
    from . import usage_alerts, usage_forecast, usage_monitor

    as_json = getattr(args, "as_json", False)
    try:
        try:
            require(calling_bot(config), "usage")
        except CommandError as exc:
            raise UsageQueryError(EXIT_REFUSED, str(exc)) from None
        try:
            node = usage_alerts.parse(condition)
        except usage_alerts.ConditionError as exc:
            raise UsageQueryError(EXIT_BAD_QUESTION, "bad condition %r: %s" % (condition, exc)) from None
        try:
            hours = _hours_spec(args)
        except CommandError as exc:
            raise UsageQueryError(EXIT_BAD_QUESTION, str(exc)) from None
        settings = usage_monitor.MonitorSettings.from_config(config)
        if getattr(args, "partial", None):
            settings.partial_windows = args.partial
        source = usage_monitor.ClaudeUsageSource(config, dict(os.environ))
        reading, cached = asyncio.run(usage_monitor.read_on_demand(settings, source))
        if not reading.ok:
            raise UsageQueryError(EXIT_UNREAD, "could not read the plan usage: %s" % reading.error)
        now = utc_now()
        evaluation = usage_monitor.Evaluation(settings, reading, now, usage_monitor.UsageHistory(settings.history_path).readings())
        values, forecast = evaluation.values(node, hours or None)
        result, reason = usage_alerts.verdict(node, values)
        extra: Dict[str, Any] = {}
        if answer is not None:
            result, reason, extra = answer(result, reason, node, values, evaluation, forecast)
    except UsageQueryError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc), "exit_code": exc.code}, ensure_ascii=False, sort_keys=True))
        else:
            print("alfred: %s" % exc, file=sys.stderr)
        return exc.code
    if not as_json:
        print("%s: %s" % ("true" if result else "false", reason))
        return EXIT_ANSWERED
    payload: Dict[str, Any] = {
        "ok": True, "result": result, "reason": reason, "condition": condition,
        "met_by": [item.text for item in node.deciding(values)] if result else [],
        "limits": _limits(reading, now),
        "values": {key.replace("weekly_all.", "week."): value for key, value in values.items()},
        "reading": {"taken_at": to_utc_text(reading.taken_at), "age_seconds": int((now - reading.taken_at).total_seconds()),
                    "cached": cached, "source": reading.source},
        "window_share": evaluation.share.to_dict(),
    }
    if forecast is not None:
        payload["forecast"] = forecast.to_dict()
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return EXIT_ANSWERED


def cmd_usage_check(config: Config, args: argparse.Namespace) -> int:
    return _usage_question(config, args, args.condition)


def running_low_condition(limit: str, threshold: float, projected: bool = False) -> str:
    """running_low as a condition of the same language: the limits at or over the threshold, and with
    `projected`, those that would run out before their reset at the recent pace."""
    kinds = ("session", "week") if limit == "any" else (limit,)
    parts = ["%s.used >= %g" % (kind, threshold) for kind in kinds]
    if projected:
        parts += ["%s.projected >= 100" % kind for kind in kinds]
    return " or ".join(parts)


def cmd_usage_running_low(config: Config, args: argparse.Namespace) -> int:
    from . import usage_monitor

    threshold = args.threshold
    if threshold is None:
        threshold = usage_monitor.MonitorSettings.from_config(config).low_threshold
    if not 0 < threshold <= 100:
        print("alfred: the threshold is a percentage between 1 and 100", file=sys.stderr)
        return EXIT_BAD_QUESTION

    def answer(result, reason, node, values, evaluation, forecast):
        low = sorted({"session" if item.window == "session" else "week" for item in node.deciding(values)}) if result else []
        return result, reason, {"low": low, "limit": args.limit, "threshold": threshold, "projected": args.projected}

    return _usage_question(config, args, running_low_condition(args.limit, threshold, args.projected), answer)


def cmd_usage_forecast(config: Config, args: argparse.Namespace) -> int:
    """Will weekly quota go unused? The same test as `usage-alert waste`, answered now."""
    condition = WASTE_CONDITION % args.margin

    def answer(result, reason, node, values, evaluation, forecast):
        if forecast is None:
            raise UsageQueryError(EXIT_UNREAD, "the reading has no weekly reset time, so there is no forecast")
        from .usage_alerts import _span

        reset = _span((forecast.week_resets_at - evaluation.now).total_seconds())
        start = "%g%% of the week is left and the %g useful 5-hour windows before it resets in %s (active hours: " \
                "%s) can spend at most %g%%" % (forecast.week_left, forecast.windows_left, reset,
                                                forecast.hours.describe(), forecast.max_spend)
        if result:
            reason = start + ": %g%% will be lost even using every window fully" % max(0.0, forecast.surplus)
        else:
            reason = start + (": all of it can still be used" if forecast.week_left > 0 else ": nothing is left")
        return result, reason, {"margin": args.margin}

    return _usage_question(config, args, condition, answer)


def cmd_service(config: Config, args: argparse.Namespace) -> int:
    """Run the bot as a background service that starts at login and restarts if it dies."""
    from . import service

    try:
        if args.action == "install":
            home.prepare(config)
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
    """Talk to one bot's agent from the terminal: its directory, memory, skills and sessions; no Telegram."""
    from .agent import Agent
    from .bots import BotRegistry, prepare_workspace

    home.prepare(config)
    record = BotRegistry(config).find(args.bot)
    if record is None:
        print("alfred: no bot called %r (there is no main agent; `alfred chat --bot <bot>` talks to one of "
              "the bots)" % args.bot, file=sys.stderr)
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

    commands.add_parser("run", help="start the control panel, the bots and the scheduler").set_defaults(run=cmd_run)
    commands.add_parser("init", help="create or refresh the home (the control panel's state)").set_defaults(run=cmd_init)

    service = commands.add_parser("service", help="keep the bot running in the background")
    service.add_argument("action", choices=("install", "uninstall", "status", "restart", "logs"))
    service.add_argument("-n", "--lines", type=int, default=40, help="log lines to show")
    service.set_defaults(run=cmd_service)

    usage_parser = commands.add_parser(
        "usage", help="show the Claude plan usage, or answer a question about it (check, running_low, forecast)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Without a question: the usage report. The questions read /usage (no model call, no quota),\n"
                    "reuse a reading a few minutes old, and print `true: <reason>` or `false: <reason>`.\n"
                    "Exit codes: 0 answered, 1 refused (no usage permission), 2 bad condition, 3 usage not read.",
    )
    usage_parser.set_defaults(run=cmd_usage)
    questions = usage_parser.add_subparsers(dest="question")

    def hours_options(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--hours", help="active hours for the forecast values, such as 9-20 or \"9-20 mon-fri\"; "
                                            "default ALFRED_USAGE_ACTIVE_HOURS, else every hour")
        parser.add_argument("--days", help="days of the active hours, such as mon-fri or sat,sun")
        parser.add_argument("--tz", help="time zone of the active hours, such as Europe/Madrid; default local time")

    check = questions.add_parser("check", help="is this condition true now? (the usage-alert language)")
    check.add_argument("condition", help='for example "week.used >= 90" or "session.left < 1h and week.surplus > 0"')
    hours_options(check)
    check.add_argument("--json", dest="as_json", action="store_true", help="the full breakdown, for logs or to report to the user")
    check.set_defaults(run=cmd_usage_check)
    for name in ("running_low", "running-low"):
        low = questions.add_parser(name, help="is a plan limit nearly used up? true means: don't start non-urgent work")
        low.add_argument("--limit", choices=("any", "session", "week"), default="any")
        low.add_argument("--threshold", type=float, help="percent used that counts as low; default "
                                                         "ALFRED_USAGE_LOW_THRESHOLD, else 90")
        low.add_argument("--projected", action="store_true",
                         help="also true when the recent pace would use a limit up before it resets")
        low.add_argument("--json", dest="as_json", action="store_true", help="the full breakdown")
        low.set_defaults(run=cmd_usage_running_low)
    forecast_parser = questions.add_parser("forecast", help="will weekly quota go unused before the reset?")
    hours_options(forecast_parser)
    forecast_parser.add_argument("--margin", type=float, default=0.0,
                                 help="points of lost quota that count as waste (default 0: the exact limit counts)")
    forecast_parser.add_argument("--partial", choices=("prorata", "full"), help="how partial windows count")
    forecast_parser.add_argument("--json", dest="as_json", action="store_true", help="windows, assumptions and the full breakdown")
    forecast_parser.set_defaults(run=cmd_usage_forecast)

    alert = commands.add_parser(
        "usage-alert", help="wake this bot when a condition on the Claude plan usage holds",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Values: session.used, week.used (percent used), session.left, week.left (time to the reset),\n"
                    "week.windows_left, week.max_spend, week.surplus (the weekly forecast for the active hours),\n"
                    "session.projected, week.projected (percent at the reset at the recent pace).\n"
                    "Operators: < <= > >= = !=, joined with and, or, not and parentheses.\n"
                    "Example: alfred usage-alert add --when \"session.left < 1h and session.used = 0\" use the spare window\n"
                    "Example: alfred usage-alert add 'week.surplus > 0' --hours 9-20\n"
                    "Example: alfred usage-alert waste --hours 9-20  (weekly quota that can't be used up)\n"
                    "An alert fires once (or with --repeat, once per window; forecast alerts once per quota week).\n"
                    "Each wake-up uses plan usage.",
    )
    alert_actions = alert.add_subparsers(dest="action", required=True)

    def alert_options(parser: argparse.ArgumentParser, repeat_help: str) -> None:
        parser.add_argument("--repeat", action="store_true", help=repeat_help)
        parser.add_argument("--hours", help="active hours for the forecast values, such as 9-20 or \"9-20 mon-fri\"")
        parser.add_argument("--days", help="days of the active hours, such as mon-fri")
        parser.add_argument("--tz", help="time zone of the active hours, such as Europe/Madrid")
        parser.add_argument("--chat-id", type=int)
        parser.add_argument("--thread-id", type=int)
        parser.add_argument("--bot", help=argparse.SUPPRESS)

    alert_add = alert_actions.add_parser("add", help="add an alert: the condition (or --when), then a note you get back")
    alert_add.add_argument("--when", help='for example "week.left < 1d and week.used < 60"')
    alert_options(alert_add, "fire again in every new window (forecast alerts: every week), not just once")
    alert_add.add_argument("note", nargs="*")
    alert_add.set_defaults(json=cmd_usage_alert_add)
    alert_waste = alert_actions.add_parser(
        "waste", help="wake this bot when weekly quota can no longer be used up before the reset")
    alert_waste.add_argument("--margin", type=float, default=0.0,
                             help="points of lost quota that count (default 0: fires at the exact limit; "
                                  "5 ignores small losses; -8 warns one window earlier)")
    alert_options(alert_waste, "arm it again every week")
    alert_waste.add_argument("note", nargs="*")
    alert_waste.set_defaults(json=cmd_usage_alert_waste)
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

    chat = commands.add_parser("chat", help="talk to one of the bots from this terminal")
    chat.add_argument("-m", "--message", help="send one message and exit")
    chat.add_argument("--chat-id", type=int)
    chat.add_argument("--bot", required=True, help="the bot to talk to, by @username, name or id")
    chat.add_argument("--backend", choices=("claude", "codex"))
    chat.add_argument("--new", action="store_true", help="start a fresh conversation")
    chat.set_defaults(run=cmd_chat)

    commands.add_parser("skills", help="list a bot's skills (inside its turn)").set_defaults(json=cmd_skills)

    send = commands.add_parser("send", help="send a message or file to a chat now")
    send.add_argument("--chat-id", type=int)
    send.add_argument("--bot", help="which bot posts it: @username or id; inside a turn, the one whose turn it is")
    send.add_argument("--thread-id", type=int, help="the topic to post in")
    send.add_argument("--file")
    send.add_argument("text", nargs="*")
    send.set_defaults(json=cmd_send)

    event = commands.add_parser("event", help="wake a bot's agent now with a note about something that happened")
    event.add_argument("--chat-id", type=int)
    event.add_argument("--bot", help="which bot's agent to wake: @username or id; inside a turn, the one whose turn it is")
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
    # The bot admin's helpers (docs/BOT_ADMIN.md and the alfred:bot-admin skill); they need the admin permission.
    for verb, handler, text in (("spec", cmd_bots_spec, "check a spec without sending anything"),
                                ("create", cmd_bots_create, "send the user a request to create a bot from a spec")):
        sub_parser = bots_actions.add_parser(verb, help=text)
        sub_parser.add_argument("--file", help="the spec, a JSON file in your own directory")
        sub_parser.add_argument("--json", dest="json_text", help="the spec as JSON text")
        sub_parser.add_argument("--chat-id", type=int, help=argparse.SUPPRESS)
        sub_parser.add_argument("--thread-id", type=int, help=argparse.SUPPRESS)
        sub_parser.set_defaults(json=handler)
    bots_status = bots_actions.add_parser("status", help="your bot requests and what became of them")
    bots_status.add_argument("request", nargs="?")
    bots_status.set_defaults(json=cmd_bots_status)
    bots_configure = bots_actions.add_parser("configure", help="set up a bot you asked for: task, model, effort, soul, reminders")
    bots_configure.add_argument("name", help="@username, name or id")
    bots_configure.add_argument("--file", help="the changes, a JSON file in your own directory")
    bots_configure.add_argument("--json", dest="json_text", help="the changes as JSON text")
    bots_configure.set_defaults(json=cmd_bots_configure)

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
    gate = add.add_argument_group("usage conditions (need the usage permission; checked before each delivery)")
    gate.add_argument("--skip-if-low", nargs="?", const="any", choices=("any", "session", "week"),
                      help="don't deliver it when a limit (the 5-hour window, the week, or any) is at the threshold")
    gate.add_argument("--threshold", type=float, help="percent used for --skip-if-low (default ALFRED_USAGE_LOW_THRESHOLD, 90)")
    gate.add_argument("--projected", action="store_true",
                      help="--skip-if-low also when the recent pace would use that limit up before it resets")
    gate.add_argument("--skip-if", metavar="CONDITION", help="don't deliver it when this usage condition holds")
    gate.add_argument("--only-if", metavar="CONDITION", help="deliver it only when this usage condition holds")
    gate.add_argument("--only-if-waste", action="store_true",
                      help="deliver it only when weekly quota will be lost even using every window fully")
    gate.add_argument("--margin", type=float, help="--only-if-waste: ignore losses under this many points")
    gate.add_argument("--hours", help="active hours for the forecast values, such as 9-20")
    gate.add_argument("--days", help="active days, such as mon-fri")
    gate.add_argument("--tz", help="time zone of the active hours, such as Europe/Madrid")
    gate.add_argument("--if-unread", choices=("run", "skip"),
                      help="when the usage can't be read: deliver (default) or skip (default with --only-if)")
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
        # Inside a bot's turn: its skills, extensions and directory, not the panel's home.
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
