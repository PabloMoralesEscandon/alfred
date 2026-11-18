"""Bot admins: bots that create and set up other bots for their owner, from a spec (docs/BOT_ADMIN.md).

A bot with the `admin` permission drafts a spec from what the user asks for (name, task, personality, preset,
model, effort, reminders) and hands it to `alfred bots create`. Everything that matters is decided here, in
Python, never by the bot: the spec is data, checked field by field, and

- nothing in it may be above what the admin itself has: each permission, its folders, its backend and
  cross-agent access (`ceiling_problems`). A spec that asks for more is refused, with every reason;
- some things, even within that ceiling, need the user's own yes after the bot exists, on a button in the
  panel's chat, after a warning (`EXTRAS`): bot admin, bots, personality and shared-memory writes, Full access
  and cross-agent access. Until then the bot runs without them (`safe_permissions`);
- the owner of every bot is the user who confirms it in Telegram, never the admin;
- an admin has limits (bots per admin, bots per user, requests per hour) and never destroys a bot;
- every request, refusal, creation, approval and later change is in the audit log.

The panel checks the request again when Telegram says the bot exists, against the admin as it is then
(`revalidate`), so a request that no longer holds starts the bot with the least instead.

The requests live in <home>/state/bots/admin-requests.json, which no bot can read or write (the home is
closed to every bot); `alfred bots` and the panel are the only writers, under a file lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import bots, presets, soul
from .bots import BotError, BotRecord, BotRegistry
from .config import Config
from .scheduler import MIN_REPEAT_SECONDS, parse_duration, parse_time, to_utc_text, utc_now

REQUESTS_FILE = "admin-requests.json"  # in <home>/state/bots/
KEEP_REQUESTS = 300
SPEC_KEYS = ("name", "username", "task", "soul", "preset", "permissions", "cross_agent", "model", "effort",
             "backend", "workdir", "folders", "reminders", "note")
CONFIGURE_KEYS = ("task", "model", "effort", "soul", "reminders")
NAME_LIMIT = 64
NOTE_LIMIT = 500
REMINDER_LIMIT = 5
REMINDER_TEXT_LIMIT = 1000
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,28}bot$", re.I)

# Limits on bot admins. 0, the default, is no limit: Telegram's own cap on bots per account still applies.
DEFAULT_MAX_PER_ADMIN = 0  # ALFRED_ADMIN_MAX_BOTS: bots one admin asked for, created or waiting
DEFAULT_MAX_PER_USER = 0  # ALFRED_ADMIN_MAX_BOTS_PER_USER: the user's bots, created or waiting
DEFAULT_PER_HOUR = 0  # ALFRED_ADMIN_REQUESTS_PER_HOUR: requests one admin sends in an hour
APPROVAL_SECONDS = 24 * 3600  # how long the button that approves the extras is honoured

# What needs the user's own yes, even within the admin's ceiling: key -> (label, warning).
SENSITIVE_SWITCHES = ("admin", "bots", "soul", "shared")
EXTRAS: Dict[str, Tuple[str, str]] = {
    "full": ("🔓 Full access (a shell with no sandbox, or bypassPermissions)",
             "it can do anything your user can on this Mac, without asking, and get around the other rules"),
    "admin": ("🧑‍💼 Bot admin", "it could ask you for more bots and set them up, as this admin does"),
    "bots": ("🤖 List, configure and request your other bots", "it could change the task and model of your other bots"),
    "soul": ("🎭 Edit its own personality", "it could rewrite its SOUL.md, and with it how it behaves"),
    "shared": ("🧠 Write the shared memory", "every other agent reads what it writes there, instructions included"),
    "cross": ("⚠️ Cross-agent access", "it could read the memory of your other bots and use their tools"),
}
STATUS_WORDS = {
    "waiting": "sent to the user, waiting for them to create it in Telegram",
    "created": "created",
    "awaiting_approval": "created; the extras wait for the user's yes",
    "approved": "created; the user approved the extras",
    "declined": "created; the user declined the extras",
    "refused": "created without the extras (the admin no longer had them)",
    "invalid": "created with the least: the request no longer held when the user confirmed",
    "cancelled": "cancelled by the user",
    "expired": "expired: the user did not create it in time",
}


class SpecError(Exception):
    """A spec that can't be accepted: every reason, in `problems`."""

    def __init__(self, problems: List[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass
class Checked:
    """A spec that passed: what the bot is asked to be, and what it starts with until the user says yes."""

    name: str
    username: str = ""
    settings: Dict[str, str] = field(default_factory=dict)
    permissions: Dict[str, str] = field(default_factory=dict)  # all it asks for (only what differs from the defaults)
    initial: Dict[str, str] = field(default_factory=dict)  # what it starts with: `permissions` without the extras
    cross_agent: bool = False
    soul: str = ""  # its own personality; empty means the base
    reminders: List[Dict[str, Any]] = field(default_factory=list)
    extras: List[str] = field(default_factory=list)  # keys of EXTRAS, waiting for the user's yes
    note: str = ""  # the admin's words to the user: shown as the admin's, never acted on
    skipped: List[str] = field(default_factory=list)  # what was left out at creation (a reminder now in the past)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -- the limits ------------------------------------------------------------------


def _number(config: Config, name: str, default: int) -> int:
    try:
        return max(0, int(config.get(name, str(default))))
    except ValueError:
        return default


def limits(config: Config) -> Dict[str, int]:
    return {
        "bots_per_admin": _number(config, "ALFRED_ADMIN_MAX_BOTS", DEFAULT_MAX_PER_ADMIN),
        "bots_per_user": _number(config, "ALFRED_ADMIN_MAX_BOTS_PER_USER", DEFAULT_MAX_PER_USER),
        "requests_per_hour": _number(config, "ALFRED_ADMIN_REQUESTS_PER_HOUR", DEFAULT_PER_HOUR),
    }


def limit_problems(config: Config, admin: BotRecord) -> List[str]:
    """Why this admin can't send another request now, if it can't. A limit of 0 is no limit."""
    registry = BotRegistry(config)
    allowed = limits(config)
    requests = load(config)
    waiting = [item for item in requests if item["status"] == "waiting"]
    mine = [record for record in registry.list(owner_id=admin.owner_id) if record.created_by == admin.bot_id]
    mine_waiting = [item for item in waiting if item["admin_bot_id"] == admin.bot_id]
    problems = []
    if allowed["bots_per_admin"] and len(mine) + len(mine_waiting) >= allowed["bots_per_admin"]:
        problems.append("limit reached: %s already has %d bot(s) created or waiting for the user, and a bot admin may "
                        "have at most %d (ALFRED_ADMIN_MAX_BOTS)" % (admin.handle, len(mine) + len(mine_waiting),
                                                                     allowed["bots_per_admin"]))
    owned = len(registry.list(owner_id=admin.owner_id)) + len(registry.pending_for(admin.owner_id))
    if allowed["bots_per_user"] and owned >= allowed["bots_per_user"]:
        problems.append("limit reached: the user already has %d bot(s) created or waiting, and bot admins may take "
                        "them to at most %d (ALFRED_ADMIN_MAX_BOTS_PER_USER); the user can still create more from "
                        "the panel" % (owned, allowed["bots_per_user"]))
    hour_ago = time.time() - 3600
    recent = [item for item in requests if item["admin_bot_id"] == admin.bot_id and item["created"] > hour_ago]
    if allowed["requests_per_hour"] and len(recent) >= allowed["requests_per_hour"]:
        problems.append("limit reached: %s sent %d request(s) in the last hour, the most allowed "
                        "(ALFRED_ADMIN_REQUESTS_PER_HOUR); try again later" % (admin.handle, len(recent)))
    return problems


# -- the ceiling: never above the admin -------------------------------------------


def _backend(config: Config, settings: Dict[str, str]) -> str:
    return settings.get("backend") or config.default_backend


def _inside(path: Path, roots: List[Path]) -> bool:
    path = Path(path).resolve()
    for root in roots:
        root = Path(root).resolve()
        if path == root or root in path.parents:
            return True
    return False


def ceiling_problems(config: Config, admin: BotRecord, permissions: Dict[str, str], cross_agent: bool,
                     settings: Dict[str, str]) -> List[str]:
    """What in these permissions and settings is more than the admin has. Empty when all of it is within."""
    problems = []
    mine, theirs = presets.values(admin.permissions), presets.values(permissions)
    for name in presets.managed():
        if presets.looser(name, mine[name], theirs[name]):
            problems.append("%s %s is more than %s has (%s %s)" % (name, theirs[name], admin.handle, name, mine[name]))
    roots = admin.working_dirs()
    for folder in [part for part in permissions.get("folders", "").split(",") if part.strip()]:
        if not _inside(Path(folder), roots):
            problems.append("folder %s is outside %s's own folders" % (folder, admin.handle))
    if settings.get("workdir") and not _inside(Path(settings["workdir"]), roots):
        problems.append("working folder %s is outside %s's own folders" % (settings["workdir"], admin.handle))
    if [rule for rule in permissions.get("allow", "").split(";") if rule.strip()]:
        problems.append("extra allow rules are set only by the user, from the panel")
    if _backend(config, settings) == "codex" and _backend(config, admin.settings) != "codex":
        problems.append("the codex backend enforces fewer rules than %s's claude one" % admin.handle)
    if cross_agent and not admin.cross_agent:
        problems.append("cross-agent access, which %s does not have" % admin.handle)
    return problems


def extras_of(permissions: Dict[str, str], cross_agent: bool) -> List[str]:
    """What these permissions hold that needs the user's own yes (keys of EXTRAS)."""
    record = BotRecord(bot_id=0, username="", name="", owner_id=0, permissions=dict(permissions))
    found = []
    if (bots.shell_is_open(record) and not record.allows("sandbox")) or record.permission("mode") == "bypassPermissions":
        found.append("full")
    found += [name for name in SENSITIVE_SWITCHES if record.allows(name)]
    if cross_agent:
        found.append("cross")
    return found


def safe_permissions(permissions: Dict[str, str], extras: List[str]) -> Dict[str, str]:
    """The permissions without the extras: what the bot runs with until the user approves them."""
    safe = dict(permissions)
    for name in SENSITIVE_SWITCHES:
        if name in extras:
            safe[name] = "off"
    if "full" in extras:
        safe["sandbox"] = "on"
        if safe.get("mode") == "bypassPermissions":
            safe["mode"] = "acceptEdits"
    return {name: value for name, value in safe.items() if value != bots.DEFAULT_PERMISSIONS.get(name)}


def extra_values(permissions: Dict[str, str], extras: List[str]) -> Dict[str, str]:
    """What approving the extras sets: their own permissions, at the values the spec asked for."""
    record = BotRecord(bot_id=0, username="", name="", owner_id=0, permissions=dict(permissions))
    values: Dict[str, str] = {}
    for name in SENSITIVE_SWITCHES:
        if name in extras:
            values[name] = "on"
    if "full" in extras:
        for name in ("shell", "sandbox", "mode"):
            values[name] = record.permission(name)
    return values


# -- reading a spec -----------------------------------------------------------------


def _text(value: Any, what: str, problems: List[str], limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        problems.append("%s must be text" % what)
        return ""
    value = value.strip()
    if len(value) > limit:
        problems.append("%s is too long (at most %d characters)" % (what, limit))
        return ""
    return value


def _reminders(value: Any, problems: List[str], allow_past: bool, skipped: List[str]) -> List[Dict[str, Any]]:
    """Reminders for the new bot, in its owner's chat: [{"in"|"at", "every"?, "message"|"task"}]."""
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append("reminders must be a list")
        return []
    if len(value) > REMINDER_LIMIT:
        problems.append("at most %d reminders" % REMINDER_LIMIT)
        return []
    found = []
    for number, item in enumerate(value, 1):
        what = "reminder %d" % number
        if not isinstance(item, dict):
            problems.append("%s must be an object" % what)
            continue
        unknown = sorted(set(item) - {"in", "at", "every", "message", "task"})
        if unknown:
            problems.append("%s: unknown field(s) %s" % (what, ", ".join(unknown)))
            continue
        if ("in" in item) == ("at" in item):
            problems.append("%s: give exactly one of in or at" % what)
            continue
        if ("message" in item) == ("task" in item):
            problems.append("%s: give exactly one of message or task" % what)
            continue
        kind = "task" if "task" in item else "message"
        text = _text(item[kind], "%s %s" % (what, kind), problems, REMINDER_TEXT_LIMIT)
        if not text:
            if isinstance(item[kind], str) and not item[kind].strip():
                problems.append("%s: the %s is empty" % (what, kind))
            continue
        reminder: Dict[str, Any] = {"kind": kind, "text": text, "in": None, "at": None, "every": None}
        try:
            if "in" in item:
                reminder["in"] = parse_duration(str(item["in"]))
            else:
                due = parse_time(str(item["at"]))
                if due <= utc_now():
                    if not allow_past:
                        problems.append("%s: %s is in the past" % (what, item["at"]))
                        continue
                    skipped.append("%s (%s, now in the past)" % (what, item["at"]))
                    continue
                reminder["at"] = to_utc_text(due)
            if item.get("every") is not None:
                reminder["every"] = parse_duration(str(item["every"]))
                if reminder["every"] < MIN_REPEAT_SECONDS:
                    problems.append("%s: every must be at least %d seconds" % (what, MIN_REPEAT_SECONDS))
                    continue
        except (ValueError, TypeError) as exc:
            problems.append("%s: %s" % (what, exc))
            continue
        found.append(reminder)
    return found


def parse_spec(config: Config, admin: BotRecord, data: Any, allow_past: bool = False) -> Checked:
    """Check a spec against the admin that sends it. Raises SpecError with every problem found.

    `config` is the panel's (or the admin's: the same home). With `allow_past`, a reminder whose time has
    passed is left out instead of refusing the spec (the user may confirm a while after the request)."""
    problems: List[str] = []
    if not isinstance(data, dict):
        raise SpecError(["the spec must be a JSON object with at least a name"])
    unknown = sorted(set(data) - set(SPEC_KEYS))
    if unknown:
        problems.append("unknown field(s): %s; a spec has: %s" % (", ".join(map(str, unknown)), ", ".join(SPEC_KEYS)))

    name = _text(data.get("name"), "name", problems, NAME_LIMIT)
    if not name:
        problems.append("the spec needs a name")
    elif any(ord(character) < 32 for character in name):
        problems.append("the name must be one line")
    username = _text(data.get("username"), "username", problems, 32).lstrip("@")
    if username and not (bots.USERNAME_MIN <= len(username) <= bots.USERNAME_MAX and USERNAME.match(username)):
        problems.append("username %r is not a Telegram bot username (5 to 32 letters, digits or _, ending in bot)" % username)
        username = ""
    note = _text(data.get("note"), "note", problems, NOTE_LIMIT)

    settings: Dict[str, str] = {}
    for key in ("task", "model", "effort", "backend", "workdir"):
        if data.get(key) in (None, ""):
            continue
        if not isinstance(data[key], str):
            problems.append("%s must be text" % key)
            continue
        try:
            value = bots.clean_setting(config, key, data[key])
        except BotError as exc:
            problems.append("%s: %s" % (key, exc))
            continue
        if value:
            settings[key] = value

    soul_text = ""
    if data.get("soul") not in (None, ""):
        if not isinstance(data["soul"], str):
            problems.append("soul must be text")
        else:
            try:
                soul_text = soul.clean(data["soul"])
            except soul.SoulError as exc:
                problems.append("soul: %s" % exc)

    preset = data.get("preset") or "basic"
    key = presets.find(preset) if isinstance(preset, str) else None
    if key is None:
        problems.append("preset %r is not one of: %s (cross-agent access is the cross_agent field)"
                        % (preset, ", ".join(presets.choices())))
        key = "basic"
    permissions = presets.apply(key, {})
    asked = data.get("permissions") or {}
    if not isinstance(asked, dict):
        problems.append("permissions must be an object, such as {\"web\": \"on\"}")
        asked = {}
    for permission, value in asked.items():
        permission = str(permission).strip().lower()
        if permission in ("folders", "workdir"):
            problems.append("give %s as its own field, not inside permissions" % permission)
            continue
        if not isinstance(value, str):
            problems.append("permission %s must be given as text, such as \"on\"" % permission)
            continue
        try:
            permissions[permission] = bots.clean_permission(config, permission, value)
        except BotError as exc:
            problems.append("permission %s: %s" % (permission, exc))
    folders = data.get("folders")
    if folders not in (None, "", []):
        if isinstance(folders, str):
            folders = [folders]
        if not isinstance(folders, list) or not all(isinstance(item, str) for item in folders):
            problems.append("folders must be a list of absolute paths")
        else:
            try:
                permissions["folders"] = bots.clean_permission(config, "folders", ",".join(folders))
            except BotError as exc:
                problems.append("folders: %s" % exc)
    permissions = {name: value for name, value in permissions.items() if value != bots.DEFAULT_PERMISSIONS.get(name)}

    cross = data.get("cross_agent", False)
    if not isinstance(cross, bool):
        problems.append("cross_agent must be true or false")
        cross = False

    skipped: List[str] = []
    reminders = _reminders(data.get("reminders"), problems, allow_past, skipped)
    if reminders and permissions.get("schedule", "on") != "on":
        problems.append("reminders need the schedule permission, which the spec turns off")

    problems += ceiling_problems(config, admin, permissions, cross, settings)
    if problems:
        raise SpecError(problems)
    extras = extras_of(permissions, cross)
    return Checked(name=name, username=username, settings=settings, permissions=permissions,
                   initial=safe_permissions(permissions, extras), cross_agent=cross, soul=soul_text,
                   reminders=reminders, extras=extras, note=note, skipped=skipped)


def parse_changes(config: Config, data: Any) -> Dict[str, Any]:
    """What `alfred bots configure` may change on a bot the admin made: task, model, effort, soul, reminders.
    Never its permissions, folders or backend: those are the user's, from the panel."""
    if not isinstance(data, dict) or not data:
        raise SpecError(["give the changes as a JSON object with any of: %s" % ", ".join(CONFIGURE_KEYS)])
    problems: List[str] = []
    unknown = sorted(set(data) - set(CONFIGURE_KEYS))
    if unknown:
        problems.append("can't change %s: a bot admin changes only %s; permissions, folders and the backend are the "
                        "user's, from the panel" % (", ".join(map(str, unknown)), ", ".join(CONFIGURE_KEYS)))
    changes: Dict[str, Any] = {}
    for key in ("task", "model", "effort"):
        if key in data:
            if not isinstance(data[key], str):
                problems.append("%s must be text" % key)
                continue
            try:
                changes[key] = bots.clean_setting(config, key, data[key])
            except BotError as exc:
                problems.append("%s: %s" % (key, exc))
    if "soul" in data:
        try:
            changes["soul"] = soul.clean(data["soul"]) if isinstance(data["soul"], str) else None
            if changes["soul"] is None:
                problems.append("soul must be text")
        except soul.SoulError as exc:
            problems.append("soul: %s" % exc)
    if "reminders" in data:
        changes["reminders"] = _reminders(data["reminders"], problems, False, [])
    if problems:
        raise SpecError(problems)
    return changes


def revalidate(config: Config, request: Dict[str, Any]) -> Tuple[Optional[BotRecord], Optional[Checked], List[str]]:
    """The request checked again now, against the admin as it is now: (admin, spec, problems)."""
    admin = BotRegistry(config).get(int(request.get("admin_bot_id") or 0))
    if admin is None:
        return None, None, ["the bot admin that asked for it no longer exists"]
    if int(admin.owner_id) != int(request.get("owner_id") or 0):
        return admin, None, ["%s no longer belongs to the user it asked" % admin.handle]
    if not admin.allows("admin"):
        return admin, None, ["%s is no longer a bot admin" % admin.handle]
    try:
        return admin, parse_spec(config, admin, request.get("input"), allow_past=True), []
    except SpecError as exc:
        return admin, None, exc.problems


# -- the requests on disk ---------------------------------------------------------


def _path(config: Config) -> Path:
    return config.bots_state_dir / REQUESTS_FILE


@contextmanager
def _locked(config: Config) -> Iterator[None]:
    path = _path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(str(path.with_name(".%s.lock" % path.name)), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read(config: Config) -> List[Dict[str, Any]]:
    try:
        items = json.loads(_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id")] if isinstance(items, list) else []


def _write(config: Config, items: List[Dict[str, Any]]) -> None:
    bots.write_private(_path(config), json.dumps(items[-KEEP_REQUESTS:], indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _expire(items: List[Dict[str, Any]]) -> bool:
    changed = False
    for item in items:
        if item.get("status") == "waiting" and time.time() - float(item.get("created", 0)) > bots.PENDING_SECONDS:
            item["status"] = "expired"
            changed = True
    return changed


def load(config: Config) -> List[Dict[str, Any]]:
    with _locked(config):
        items = _read(config)
        if _expire(items):
            _write(config, items)
    return items


def add(config: Config, request: Dict[str, Any]) -> Dict[str, Any]:
    with _locked(config):
        items = _read(config)
        _expire(items)
        items.append(request)
        _write(config, items)
    return request


def update(config: Config, request_id: str, **changes: Any) -> Optional[Dict[str, Any]]:
    with _locked(config):
        items = _read(config)
        found = None
        for item in items:
            if item["id"] == request_id:
                item.update(changes)
                item["updated"] = time.time()
                found = item
        if found is not None:
            _write(config, items)
    return found


def get(config: Config, request_id: str) -> Optional[Dict[str, Any]]:
    return next((item for item in load(config) if item["id"] == request_id), None)


def for_bot(config: Config, bot_id: int) -> Optional[Dict[str, Any]]:
    """The request a bot was created from, if any."""
    found = [item for item in load(config) if int(item.get("bot_id") or 0) == int(bot_id)]
    return found[-1] if found else None


def new_request(admin: BotRecord, checked: Checked, data: Dict[str, Any], username: str, chat_id: int,
                thread_id: Optional[int]) -> Dict[str, Any]:
    return {
        "id": "r" + secrets.token_hex(4),
        "admin_bot_id": admin.bot_id,
        "admin": admin.handle,
        "owner_id": admin.owner_id,
        "chat_id": int(chat_id),  # the admin's chat with the user: where it is told what happened
        "thread_id": thread_id,
        "created": time.time(),
        "status": "waiting",
        "username": username,
        "name": checked.name,
        "input": data,  # checked again, as it is, when the user confirms
        "spec": checked.as_dict(),
        "bot_id": 0,
        "bot": "",
    }


def public(request: Dict[str, Any], registry: Optional[BotRegistry] = None) -> Dict[str, Any]:
    """A request as `alfred bots status` shows it to the admin."""
    shown = {
        "id": request["id"],
        "status": request["status"],
        "meaning": STATUS_WORDS.get(request["status"], request["status"]),
        "name": request.get("name"),
        "username": request.get("username"),
        "asked_utc": datetime.fromtimestamp(float(request.get("created", 0)), timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z"),
        "extras": [EXTRAS[key][0] for key in request.get("spec", {}).get("extras", []) if key in EXTRAS],
    }
    if request.get("bot_id"):
        shown["bot"] = request.get("bot")
        record = registry.get(int(request["bot_id"])) if registry is not None else None
        if record is not None:
            shown["preset"] = presets.level_name(record)
            # Whether the user has pressed Start in the new bot yet: until then it can't write to them.
            shown["started"] = int(record.owner_id) in record.chat_ids
        else:
            shown["gone"] = True
    if request.get("problems"):
        shown["problems"] = request["problems"]
    return shown


# -- words for the user and the admin -----------------------------------------------


def reminder_line(reminder: Dict[str, Any]) -> str:
    if reminder.get("in") is not None:
        when = "in %s" % _duration(int(reminder["in"]))
    else:
        when = "at %s" % parse_time(reminder["at"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    every = ", every %s" % _duration(int(reminder["every"])) if reminder.get("every") else ""
    kind = "task" if reminder["kind"] == "task" else "message"
    return "%s%s (%s): %s" % (when, every, kind, reminder["text"][:200])


def _duration(seconds: int) -> str:
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def summary(config: Config, admin: BotRecord, checked: Checked, username: str, request_id: str) -> str:
    """The request as the user reads it in the panel's chat, before creating the bot."""
    draft = BotRecord(bot_id=0, username=username, name=checked.name, owner_id=admin.owner_id,
                      settings=dict(checked.settings), permissions=dict(checked.initial))
    lines = [
        "🧑‍💼 %s, your bot admin, asks to create a bot for you (request %s)." % (admin.handle, request_id),
        "",
        "Name: %s" % checked.name,
        "Username: @%s (you can change it in Telegram's window)" % username,
        "Task: %s" % (checked.settings.get("task") or "(none)"),
        "Personality: %s" % ("its own, %d characters" % len(checked.soul) if checked.soul else "the base"),
        "Model: %s · Effort: %s · Backend: %s" % (checked.settings.get("model") or "default",
                                                 checked.settings.get("effort") or "default",
                                                 checked.settings.get("backend") or config.default_backend),
    ]
    if checked.settings.get("workdir"):
        lines.append("Working folder: %s" % checked.settings["workdir"])
    lines += ["Permissions: %s" % presets.name_of(checked.initial), bots.permission_summary(draft, for_owner=True)]
    if checked.reminders:
        lines.append("Reminders in its chat with you:")
        lines += ["  • %s" % reminder_line(reminder) for reminder in checked.reminders]
    if checked.extras:
        lines += ["", "It also asks for these, which it will NOT have unless you approve them on a separate button "
                      "once the bot exists:"]
        lines += ["  %s: %s" % EXTRAS[key] for key in checked.extras]
    if checked.note:
        lines += ["", "%s's note (its words, not mine): %s" % (admin.handle, checked.note)]
    lines += ["", "You will own it. %s can set up its task, model, personality and reminders, but can't give it "
                  "more permissions or destroy it; that stays with you, in /bot." % admin.handle]
    return "\n".join(lines)


def approval_text(record: BotRecord, request: Dict[str, Any]) -> str:
    extras = request.get("spec", {}).get("extras", [])
    lines = ["⚠️ %s asked for more for %s (request %s). It runs without these until you say yes:" % (
        request.get("admin"), record.handle, request["id"]), ""]
    lines += ["%s: %s." % EXTRAS[key] for key in extras if key in EXTRAS]
    if "cross" in extras:
        lines += ["", bots.CROSS_AGENT_WARNING.format(name=record.name, handle=record.handle, username=record.username)]
    lines += ["", "Approve only if you trust %s with all of it. Your answer goes to the audit log. If this button "
                  "expires, grant them yourself from /bot → My bots → Permissions." % record.handle]
    return "\n".join(lines)


def created_event(record: BotRecord, request: Dict[str, Any], checked: Optional[Checked], problems: List[str]) -> str:
    """What the admin is woken with when the bot it asked for exists."""
    if checked is None:
        return ("[Consiglieri panel] The user created %s from your request %s, but the request no longer held (%s), so "
                "it started with the least (Basic) and none of your spec. Tell the user; only they can set it up now, "
                "from /bot." % (record.handle, request["id"], "; ".join(problems)))
    lines = ["[Consiglieri panel] The user created %s (%s) from your request %s. Its owner is the user." % (
        record.handle, record.name, request["id"]),
        "It has: %s." % presets.level_name(record)]
    if checked.soul:
        lines.append("Its personality (SOUL.md) is the one in your spec.")
    if checked.reminders:
        lines.append("%d reminder(s) scheduled in its chat with the user." % len(checked.reminders))
    if checked.skipped:
        lines.append("Left out: %s." % "; ".join(checked.skipped))
    if checked.extras:
        lines.append("Waiting for the user's yes in the panel: %s. Do not ask them to hurry." % ", ".join(
            EXTRAS[key][0] for key in checked.extras))
    lines.append("Next: check it with `alfred bots status %s`, adjust task, model, effort, personality or reminders "
                 "with `alfred bots configure %s` if needed, and tell the user to open %s and press Start "
                 "(it can't write to them before that)." % (request["id"], record.username, record.link))
    return "\n".join(lines)


def answered_event(record: BotRecord, request: Dict[str, Any], status: str, detail: str = "") -> str:
    words = {"approved": "approved the extra permissions you asked for", "declined": "declined the extra permissions",
             "refused": "could not approve the extras, because you no longer have them",
             "cancelled": "cancelled the request; no bot was created"}
    subject = record.handle if record is not None else request.get("name")
    return "[Consiglieri panel] About %s (request %s): the user %s.%s" % (
        subject, request["id"], words.get(status, status), " %s" % detail if detail else "")


def reminder_due(reminder: Dict[str, Any]) -> datetime:
    if reminder.get("in") is not None:
        return utc_now() + timedelta(seconds=int(reminder["in"]))
    return parse_time(reminder["at"])


def schedule_reminders(config: Config, record: BotRecord, reminders: List[Dict[str, Any]]) -> List[str]:
    """Schedule reminders for a bot, in its owner's chat with it. Returns their ids."""
    from .scheduler import KIND_MESSAGE, KIND_TASK, SchedulerStore

    store = SchedulerStore(config.db_path)
    ids = []
    for reminder in reminders:
        item = store.add(chat_id=int(record.owner_id), due=reminder_due(reminder), text=reminder["text"],
                         kind=KIND_TASK if reminder["kind"] == "task" else KIND_MESSAGE,
                         repeat_seconds=reminder.get("every"), user_id=int(record.owner_id), bot_id=record.bot_id)
        ids.append(item["id"])
    return ids


def creation_messages(intro: str, name: str, username: str, link: str) -> List[Tuple[str, Any]]:
    """The two messages that create a bot, as (text, reply_markup): first Telegram's own creation button on a
    keyboard (KeyboardButtonRequestManagedBot, with Cancel), then the creation link as the second option.
    python-telegram-bot 22.5 has no class for the button, so it carries the Bot API's own JSON."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

    request = {"request_id": bots.new_request_id(), "suggested_name": name[:64], "suggested_username": username}
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("Create @%s" % username, api_kwargs={"request_managed_bot": request})],
         [KeyboardButton(CANCEL_CREATION)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    return [
        (intro, keyboard),
        ("Second option, the link: %s" % link,
         InlineKeyboardMarkup([[InlineKeyboardButton("Create @%s by link" % username, url=link)]])),
    ]


CANCEL_CREATION = "Cancel"  # the second key of the creation keyboard
