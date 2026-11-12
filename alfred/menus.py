"""The control panel's menus: a step-by-step "create a bot" assistant and an editor for existing bots.

Everything here is Telegram-free. A screen is text plus rows of buttons, each button a label and an
action; `ControlPanel` turns them into an inline keyboard and edits one message in place.

State lives in memory, one session per user, never in the buttons: a button carries only
`m:<session token>:<action>`, well under Telegram's 64-byte limit for callback data. A session expires
after `SESSION_SECONDS` without use, and every session is gone after a restart; a button from a session
that no longer exists says so and asks for /bot again, instead of acting on stale choices.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import bots, presets, usage_monitor
from .bots import BotError, BotRecord, DEFAULT_PERMISSIONS

SESSION_SECONDS = 30 * 60
CALLBACK_LIMIT = 64  # bytes, Telegram's limit for callback_data
PREFIX = "m"

Row = List[Tuple[str, str]]  # (label, action)

# What the model step offers. The empty value means "Alfred's default" (.env, or the backend's own).
CLAUDE_MODELS = (("Default", ""), ("Fable", "fable"), ("Opus", "opus"), ("Sonnet", "sonnet"), ("Haiku", "haiku"))
CODEX_MODELS = (("Default", ""),)
CLAUDE_EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("", "low", "medium", "high")
# The modes the selector cycles through. plan and default are left to /bot perm: with nobody there to
# answer, plan never acts and default denies whatever would ask.
MODE_CYCLE = ("dontAsk", "acceptEdits", "auto", "bypassPermissions")

# The presets themselves are in presets.py. The switches the Customize screen shows, in order: (permission, label).
SWITCH_LABELS = (
    ("edit", "📁 Edit its working folder"),
    ("web", "🌐 Web search and pages"),
    ("browser", "🧭 Browser"),
    ("schedule", "⏰ Reminders and tasks"),
    ("contact", "✉️ Write to other chats"),
    ("bots", "🤖 List and request bots"),
    ("usage", "📊 Plan usage and alerts"),
)

WIZARD = ("new_name", "new_task", "new_model", "new_effort", "new_perms", "new_summary")
STEP_TITLES = {
    "new_name": "Step 1 of 6: name",
    "new_task": "Step 2 of 6: task",
    "new_model": "Step 3 of 6: model",
    "new_effort": "Step 4 of 6: effort",
    "new_perms": "Step 5 of 6: permissions",
    "new_summary": "Step 6 of 6: check and create",
}


@dataclass
class Screen:
    text: str
    rows: List[Row] = field(default_factory=list)


@dataclass
class Session:
    token: str
    user_id: int
    chat_id: int
    message_id: Optional[int] = None
    screen: str = "home"
    awaiting: Optional[str] = None  # what a typed message answers: name, task, model, bot_task, bot_model
    name: str = ""
    settings: Dict[str, str] = field(default_factory=dict)
    permissions: Dict[str, str] = field(default_factory=dict)
    bot_id: int = 0  # the bot being edited
    preset: str = ""  # the preset the draft's permissions started from
    pending: str = ""  # a preset waiting for "yes" before it is applied to a bot
    notice: str = ""  # one line shown above the next screen, then cleared
    touched: float = field(default_factory=time.time)

    def draft(self, username: str = "") -> BotRecord:
        return BotRecord(bot_id=0, username=username, name=self.name or "(unnamed)",
                         owner_id=self.user_id, settings=dict(self.settings), permissions=dict(self.permissions))


def callback(token: str, action: str) -> str:
    data = "%s:%s:%s" % (PREFIX, token, action)
    if len(data.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError("callback data too long: %r" % data)
    return data


def parse_callback(data: str) -> Optional[Tuple[str, str]]:
    """(token, action) from a menu button's data, or None for any other button."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    return parts[1], parts[2]


def _mark(on: bool) -> str:
    return "✅" if on else "⬜"


def permission_warnings(record: BotRecord, backend: str = "claude") -> List[str]:
    """Plain warnings for the risky choices, shown on the permissions and summary screens."""
    warnings = []
    if record.cross_agent:
        warnings.append("⚠️ CROSS-AGENT ACCESS: it reads the memory of the other bots and of Alfred and uses their tools.")
    if record.permission("mode") == "bypassPermissions":
        warnings.append("⚠️ bypassPermissions: no checks at all except the deny rules. Use it only in a container or VM.")
    if bots.shell_is_open(record) and not record.allows("sandbox"):
        warnings.append("⚠️ Open shell without the sandbox: it can do anything your user can, whatever the other switches "
                        "say, including reading other agents' memory and running their tools. The blacklist then "
                        "only binds Claude Code's file tools and the commands it recognizes.")
    elif record.permission("shell") == "full":
        warnings.append("⚠️ Full shell: any command, inside Claude Code's sandbox (writes only in its folders).")
    elif bots.shell_is_open(record):
        warnings.append("ℹ️ This mode runs shell commands without an allow rule, so they run in the sandbox.")
    if backend == "codex":
        warnings.append("⚠️ Codex: its sandbox can read every file of your user, so the blacklist on other agents' "
                        "memory and tools is not enforced; only the manual asks it to stay out.")
    if record.allows("browser"):
        warnings.append("⚠️ Browser: it uses the sessions you are logged in to there.")
    if record.allows("contact"):
        warnings.append("⚠️ Other chats: it can message people other than you.")
    if record.permission("mode") == "auto":
        warnings.append("ℹ️ auto: a classifier model decides what runs without asking.")
    return warnings


class Menus:
    """Sessions and screens. `pool` is the BotPool; `request_bot` sends the creation request for a draft."""

    def __init__(
        self,
        pool: Any,
        request_bot: Callable[[int, int, str, Dict[str, str], Dict[str, str]], Awaitable[str]],
    ) -> None:
        self.pool = pool
        self.request_bot = request_bot
        self.sessions: Dict[int, Session] = {}  # by user id: one open menu per person

    # -- sessions --

    def open(self, user_id: int, chat_id: int) -> Tuple[Session, Screen]:
        """A fresh menu for this user. Any earlier one is closed, so its buttons stop working."""
        session = Session(token=secrets.token_urlsafe(6), user_id=int(user_id), chat_id=int(chat_id))
        self.sessions[session.user_id] = session
        return session, self.render(session)

    def find(self, token: str, user_id: int) -> Optional[Session]:
        session = self.sessions.get(int(user_id))
        if session is None or session.token != token:
            return None
        if time.time() - session.touched > SESSION_SECONDS:
            self.sessions.pop(int(user_id), None)
            return None
        session.touched = time.time()
        return session

    def waiting_for_text(self, user_id: int) -> Optional[Session]:
        session = self.sessions.get(int(user_id))
        if session is None or session.awaiting is None or time.time() - session.touched > SESSION_SECONDS:
            return None
        return session

    def close(self, session: Session) -> None:
        if self.sessions.get(session.user_id) is session:
            del self.sessions[session.user_id]

    # -- input --

    async def press(self, session: Session, action: str) -> Optional[Screen]:
        """A button was pressed. Returns the screen to show, or None when the menu is closed."""
        verb, _, argument = action.partition(":")
        try:
            return await self._press(session, verb, argument)
        except BotError as exc:
            session.notice = "⚠️ %s" % exc
            return self.render(session)

    async def _press(self, session: Session, verb: str, argument: str) -> Optional[Screen]:
        session.awaiting = None
        if verb == "cancel":
            self.close(session)
            return None
        if verb == "home":
            return self.go(session, "home")
        if verb == "back":
            return self.go(session, self.parent(session.screen))
        if verb == "go":
            if argument == "new_name":
                session.name, session.settings = "", {}
                session.preset, session.permissions = presets.RECOMMENDED, presets.apply(presets.RECOMMENDED, {})
            return self.go(session, argument)
        if verb == "skip":  # the task is optional
            session.settings.pop("task", None)
            return self.go(session, "new_model")
        if verb == "next":
            if session.screen == "new_perms_custom":
                return self.go(session, "new_summary")
            return self.go(session, WIZARD[WIZARD.index(session.screen) + 1])
        if verb == "backend":
            return await self._choose(session, "backend", argument)
        if verb == "model":
            return await self._choose(session, "model", argument)
        if verb == "effort":
            return await self._choose(session, "effort", argument)
        if verb == "typemodel":
            session.awaiting = "bot_model" if session.screen == "bot_model" else "model"
            return self.render(session)
        if verb in ("perm", "shell", "mode"):
            return await self._permission(session, verb, argument)
        if verb in ("pick", "pickok"):
            return await self._pick(session, argument if verb == "pick" else session.pending, confirmed=verb == "pickok")
        if verb == "custom":
            return self.go(session, "bot_perms_custom" if session.screen.startswith("bot_") else "new_perms_custom")
        if verb == "cross":
            return self.go(session, "bot_cross")
        if verb in ("crossgrant", "crossrevoke"):
            record = self._record(session)
            if verb == "crossgrant" and session.screen != "bot_cross":
                return self.go(session, "bot_cross")  # granting always goes through the warning
            updated = await self.pool.set_cross_agent(record, verb == "crossgrant", session.user_id, "menu")
            session.notice = ("Cross-agent access granted. It applies from its next message; withdraw it here any time."
                              if updated.cross_agent else "Cross-agent access withdrawn. It applies from its next message.")
            return self.go(session, "bot_perms")
        if verb == "create":
            if not session.name:
                session.notice = "⚠️ Give it a name first."
                return self.go(session, "new_name")
            username = await self.request_bot(session.user_id, session.chat_id, session.name, session.settings, session.permissions)
            self.close(session)
            return Screen(
                "Request sent for %s (@%s). Tap \"Create @%s\" on the keyboard I just sent and confirm in "
                "Telegram; the link below it is the second option. I'll tell you here when it is ready."
                % (session.name, username, username)
            )
        if verb == "bot":
            session.bot_id = int(argument)
            return self.go(session, "bot")
        if verb == "edittask":
            session.awaiting = "bot_task"
            return self.go(session, "bot_task")
        if verb in ("pause", "resume"):
            record = self._record(session)
            if verb == "pause":
                await self.pool.pause(record)
                session.notice = "Paused. Messages sent to it meanwhile are dropped; its reminders wait."
            else:
                session.notice = await self.pool.resume(record)
            return self.go(session, "bot")
        if verb == "destroy":
            return self.go(session, "bot_destroy")
        if verb == "destroy!":
            record = self._record(session)
            report = await self.pool.destroy(record)
            session.bot_id = 0
            session.notice = report
            return self.go(session, "list")
        return self.render(session)

    async def text(self, session: Session, text: str) -> Screen:
        """The user typed something the menu asked for."""
        text = text.strip()
        what, session.awaiting = session.awaiting, None
        try:
            if what == "name":
                if not text or len(text) > 64 or "\n" in text:
                    session.notice = "⚠️ A name is one line of 1 to 64 characters."
                    session.awaiting = "name"
                    return self.render(session)
                session.name = text
                return self.go(session, "new_task")
            if what == "task":
                session.settings["task"] = bots.clean_setting(self.pool.config, "task", text)
                return self.go(session, "new_model")
            if what == "model":
                session.settings["model"] = bots.clean_setting(self.pool.config, "model", text)
                return self.go(session, "new_effort")
            if what == "bot_task":
                await self.pool.set_setting(self._record(session), "task", text)
                session.notice = "Task changed. It applies from its next message."
                return self.go(session, "bot")
            if what == "bot_model":
                await self.pool.set_setting(self._record(session), "model", text)
                session.notice = "Model changed. It applies from its next message."
                return self.go(session, "bot")
        except BotError as exc:
            session.notice = "⚠️ %s" % exc
            session.awaiting = what
        return self.render(session)

    # -- choices --

    async def _choose(self, session: Session, name: str, argument: str) -> Screen:
        """A model, effort or backend button: on the draft in the wizard, on the bot in the editor."""
        editing = session.screen.startswith("bot_")
        current = self._record(session).settings if editing else session.settings
        backend = current.get("backend") or self.pool.config.default_backend
        if name == "backend":
            value = ("claude", "codex")[int(argument)]
        elif name == "model":
            value = (CODEX_MODELS if backend == "codex" else CLAUDE_MODELS)[int(argument)][1]
        else:
            value = (CODEX_EFFORTS if backend == "codex" else CLAUDE_EFFORTS)[int(argument)]
        if editing:
            record = self._record(session)
            await self.pool.set_setting(record, name, value or "default")
            if name == "backend":  # models and efforts differ between backends
                await self.pool.set_setting(record, "model", "default")
                await self.pool.set_setting(record, "effort", "default")
                return self.render(session)
            session.notice = "%s set to %s. It applies from its next message." % (name.capitalize(), value or "the default")
            return self.go(session, "bot")
        if value:
            session.settings[name] = value
        else:
            session.settings.pop(name, None)
        if name == "backend":
            session.settings.pop("model", None)
            session.settings.pop("effort", None)
            return self.render(session)
        return self.go(session, "new_effort" if name == "model" else "new_perms")

    async def _permission(self, session: Session, verb: str, argument: str) -> Screen:
        editing = session.screen.startswith("bot_")
        record = self._record(session) if editing else session.draft()
        changes: Dict[str, str] = {}
        if verb == "shell":
            changes["shell"] = "alfred" if record.permission("shell") == "full" else "full"
        elif verb == "mode":
            current = record.permission("mode")
            position = MODE_CYCLE.index(current) if current in MODE_CYCLE else -1
            changes["mode"] = MODE_CYCLE[(position + 1) % len(MODE_CYCLE)]
        else:
            if argument not in bots.SWITCHES:
                raise BotError("unknown switch %r" % argument)
            changes[argument] = "off" if record.allows(argument) else "on"
        if editing:
            for key, value in changes.items():
                await self.pool.set_permission(record, key, value)
        else:
            for key, value in changes.items():
                session.permissions[key] = bots.clean_permission(self.pool.config, key, value)
            session.permissions = {k: v for k, v in session.permissions.items() if v != DEFAULT_PERMISSIONS[k]}
        return self.render(session)

    async def _pick(self, session: Session, key: str, confirmed: bool = False) -> Screen:
        """A preset button. In the assistant it is chosen and the summary follows; on a bot it is applied,
        after a confirmation when it grants anything the bot does not have yet."""
        if key not in presets.PRESET_PERMISSIONS:
            raise BotError("unknown preset %r" % key)
        session.pending = ""
        if not session.screen.startswith("bot_"):
            session.preset, session.permissions = key, presets.apply(key, session.permissions)
            return self.go(session, "new_summary")
        record = self._record(session)
        target = presets.apply(key, record.permissions)
        if presets.detect(record.permissions)[0] == key:
            session.notice = "%s already has %s." % (record.handle, presets.title(key))
            return self.go(session, "bot_perms")
        if presets.gains(record.permissions, target) and not confirmed:
            session.pending = key
            return self.go(session, "bot_perms_confirm")
        await self.pool.set_permissions(record, target)
        session.notice = "%s now has %s. It applies from its next message." % (record.handle, presets.title(key))
        return self.go(session, "bot_perms")

    def _record(self, session: Session) -> BotRecord:
        record = self.pool.registry.get(session.bot_id) if session.bot_id else None
        if record is None:
            session.bot_id = 0
            raise BotError("that bot no longer exists")
        return record

    # -- navigation --

    @staticmethod
    def parent(screen: str) -> str:
        if screen == "new_perms_custom":
            return "new_perms"
        if screen in ("bot_perms_custom", "bot_perms_confirm"):
            return "bot_perms"
        if screen in WIZARD:
            index = WIZARD.index(screen)
            return WIZARD[index - 1] if index else "home"
        if screen == "bot_cross":
            return "bot_perms"
        if screen.startswith("bot_"):
            return "bot"
        if screen == "bot":
            return "list"
        return "home"

    def go(self, session: Session, screen: str) -> Screen:
        session.screen = screen
        session.awaiting = {"new_name": "name", "new_task": "task", "bot_task": "bot_task"}.get(screen, session.awaiting)
        return self.render(session)

    # -- screens --

    def render(self, session: Session) -> Screen:
        screen = getattr(self, "_screen_%s" % session.screen)(session)
        notice, session.notice = session.notice, ""
        if notice:
            screen.text = "%s\n\n%s" % (notice, screen.text)
        token = session.token
        screen.rows = [[(label, callback(token, action)) for label, action in row] for row in screen.rows]
        return screen

    def _screen_home(self, session: Session) -> Screen:
        return Screen(
            "Alfred's control panel. Each bot has its own chat, task, model, permissions, memory and session.",
            [[("➕ Create bot", "go:new_name")], [("🤖 My bots", "go:list")], [("❓ Help", "go:help")],
             [("✖️ Close", "cancel")]],
        )

    def _screen_help(self, session: Session) -> Screen:
        return Screen(
            "How it works\n\n"
            "Create bot walks you through name, task, model, effort and permissions, then sends a button "
            "that asks Telegram to create the bot. Confirm there and the bot is ready in its own chat.\n\n"
            "My bots lets you change a bot's model, effort, task and permissions, pause it, resume it or "
            "destroy it.\n\n"
            "What a bot may do is a security preset: %s, %s, %s or %s, from least to most. Customize turns "
            "single permissions on and off; the bot then shows as Custom. Claude Code enforces the permissions; "
            "with a full shell and no sandbox a bot can do anything your user can.\n\n"
            "At every level a bot is kept out of other agents' memory and tools (other bots', Alfred's). "
            "Cross-agent access, above full access, lifts that: grant it in My bots → Permissions, after a "
            "warning. Every grant and withdrawal is logged (/bot audit).\n\n"
            "Text shortcuts still work: /bot help lists them." % tuple(presets.title(key) for key in presets.keys()),
            [[("⬅️ Back", "back")]],
        )

    def _nav(self, *extra: Tuple[str, str]) -> Row:
        return list(extra) + [("⬅️ Back", "back"), ("✖️ Cancel", "cancel")]

    def _screen_new_name(self, session: Session) -> Screen:
        current = "\nNow: %s" % session.name if session.name else ""
        rows = [self._nav(("Next ➡️", "next"))] if session.name else [self._nav()]
        return Screen("%s\n\nSend the bot's name as a message, for example Research.%s" % (STEP_TITLES["new_name"], current), rows)

    def _screen_new_task(self, session: Session) -> Screen:
        task = session.settings.get("task")
        current = "\nNow: %s" % task if task else ""
        rows = [[("Skip, no task yet", "skip")]]
        if task:
            rows[0].append(("Keep it ➡️", "next"))
        return Screen(
            "%s\n\nSend what %s should do, as a message. It goes into its manual.%s"
            % (STEP_TITLES["new_task"], session.name, current),
            rows + [self._nav()],
        )

    def _model_rows(self, settings: Dict[str, str], backend: str) -> List[Row]:
        choices = CODEX_MODELS if backend == "codex" else CLAUDE_MODELS
        chosen = settings.get("model", "")
        rows: List[Row] = [[("%sClaude" % ("● " if backend == "claude" else ""), "backend:0"),
                            ("%sCodex" % ("● " if backend == "codex" else ""), "backend:1")]]
        buttons = [("%s%s" % ("● " if value == chosen else "", label), "model:%d" % index)
                   for index, (label, value) in enumerate(choices)]
        rows += [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([("✏️ Type a model name", "typemodel")])
        return rows

    def _effort_rows(self, settings: Dict[str, str], backend: str) -> List[Row]:
        choices = CODEX_EFFORTS if backend == "codex" else CLAUDE_EFFORTS
        chosen = settings.get("effort", "")
        buttons = [("%s%s" % ("● " if value == chosen else "", value or "Default"), "effort:%d" % index)
                   for index, value in enumerate(choices)]
        return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]

    def _backend(self, settings: Dict[str, str]) -> str:
        return settings.get("backend") or self.pool.config.default_backend

    def _screen_new_model(self, session: Session) -> Screen:
        backend = self._backend(session.settings)
        typing = "\n\nSend the model's name as a message." if session.awaiting == "model" else ""
        return Screen(
            "%s\n\nWhich model should %s use? Default means Alfred's setting.%s"
            % (STEP_TITLES["new_model"], session.name, typing),
            self._model_rows(session.settings, backend) + [self._nav(("Next ➡️", "next"))],
        )

    def _screen_new_effort(self, session: Session) -> Screen:
        return Screen(
            "%s\n\nHow hard should it think? More effort is slower and uses more of your plan." % STEP_TITLES["new_effort"],
            self._effort_rows(session.settings, self._backend(session.settings)) + [self._nav(("Next ➡️", "next"))],
        )

    def _permission_rows(self, record: BotRecord, editing: bool = False) -> List[Row]:
        rows: List[Row] = [[("✅ Its own folder (always)", "noop")]]
        for name, label in SWITCH_LABELS:
            rows.append([("%s %s" % (_mark(record.allows(name)), label), "perm:%s" % name)])
        full = record.permission("shell") == "full"
        rows.append([("%s Shell: %s" % ("⚠️" if full else "🐚", "any command" if full else "only alfred"), "shell")])
        if bots.shell_is_open(record):
            rows.append([("%s Sandbox for the shell" % (_mark(record.allows("sandbox")) if record.allows("sandbox") else "⚠️ ⬜"), "perm:sandbox")])
        mode = record.permission("mode")
        rows.append([("%s Mode: %s" % ("⚠️" if mode == "bypassPermissions" else "🔐", mode), "mode")])
        if editing:  # never at creation: only for a bot that exists, and always through the warning
            rows.append(self._cross_row(record))
        return rows

    @staticmethod
    def _cross_row(record: BotRecord) -> Row:
        """Cross-agent access, the level above Full access: its own button, never a preset or a switch."""
        if record.cross_agent:
            return [("⚠️ CROSS-AGENT ACCESS ON · withdraw", "crossrevoke")]
        return [("🔒 Cross-agent access: off (above full access)", "cross")]

    def _permission_text(self, record: BotRecord) -> str:
        lines = [bots.permission_summary(record, for_owner=True)]
        warnings = permission_warnings(record, self._backend(record.settings))
        note = usage_monitor.permission_note(self.pool.config, record) if self.pool is not None else ""
        if note:
            warnings.append(note)
        if warnings:
            lines += [""] + warnings
        return "\n".join(lines)

    def _preset_rows(self, permissions: Dict[str, str]) -> List[Row]:
        """One button per preset, the current one marked, then Customize."""
        current = presets.detect(permissions)[0]
        rows: List[Row] = [[("%s%s" % ("● " if key == current else "", presets.title(key, recommended_mark=True)), "pick:%s" % key)]
                           for key in presets.keys()]
        rows.append([("%s Customize" % presets.CUSTOM_ICON, "custom")])
        return rows

    @staticmethod
    def _preset_cards() -> str:
        return "\n\n".join(presets.card(key) for key in presets.keys())

    def _screen_new_perms(self, session: Session) -> Screen:
        return Screen(
            "%s\n\nWhat may %s do? Pick a preset, or Customize to set each permission yourself. At every level, "
            "even Full access, it can't read other agents' memory or use their tools; cross-agent access, above "
            "Full access, is granted only to a bot that exists (My bots → Permissions).\n\n%s\n\nNow: %s"
            % (STEP_TITLES["new_perms"], session.name, self._preset_cards(),
               presets.name_of(session.permissions, session.preset)),
            self._preset_rows(session.permissions) + [self._nav()],
        )

    def _screen_new_perms_custom(self, session: Session) -> Screen:
        draft = session.draft()
        return Screen(
            "%s, customized\n\nTap to switch. Starting point: %s.\nNow: %s\n\n%s"
            % (STEP_TITLES["new_perms"], presets.title(session.preset or presets.RECOMMENDED),
               presets.name_of(session.permissions, session.preset), self._permission_text(draft)),
            self._permission_rows(draft) + [self._nav(("Next ➡️", "next"))],
        )

    def _settings_lines(self, settings: Dict[str, str]) -> List[str]:
        return [
            "Task: %s" % (settings.get("task") or "none yet"),
            "Backend: %s" % self._backend(settings),
            "Model: %s" % (settings.get("model") or "default"),
            "Effort: %s" % (settings.get("effort") or "default"),
        ]

    def _screen_new_summary(self, session: Session) -> Screen:
        draft = session.draft(self.pool.suggest_username(session.name, session.settings.get("task", "")))
        lines = [STEP_TITLES["new_summary"], "", "Name: %s" % draft.name,
                 "Username: @%s (proposed; you can change both in Telegram's window)" % draft.username]
        lines += self._settings_lines(session.settings)
        lines += ["Permissions: %s" % presets.name_of(session.permissions, session.preset), "", self._permission_text(draft)]
        return Screen("\n".join(lines), [[("✅ Create", "create")], self._nav()])

    def _screen_list(self, session: Session) -> Screen:
        records = self.pool.registry.list()
        rows: List[Row] = []
        for record in records:
            state = "" if record.enabled else " (paused)"
            if record.enabled and not self.pool.is_running(record.bot_id):
                state = " (not connected)"
            flag = " ⚠️ CROSS-AGENT" if record.cross_agent else ""
            rows.append([("%s %s%s%s" % (record.name, record.handle, state, flag), "bot:%d" % record.bot_id)])
        text = "Your bots. Tap one to change it." if records else "No bots yet."
        return Screen(text, rows + [[("➕ Create bot", "go:new_name"), ("⬅️ Back", "back")]])

    def _screen_bot(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        lines = ["%s (%s), %s" % (record.name, record.handle, "on" if record.enabled else "paused"), record.link, ""]
        if record.cross_agent:
            lines += [bots.cross_agent_line(record), ""]
        lines += self._settings_lines(record.settings)
        if record.settings.get("workdir"):
            lines.append("Works in: %s" % record.settings["workdir"])
        lines += ["Permissions: %s" % presets.level_name(record), "", self._permission_text(record)]
        rows: List[Row] = [
            [("🧠 Model", "go:bot_model"), ("⚙️ Effort", "go:bot_effort")],
            [("📝 Task", "edittask"), ("🔐 Permissions", "go:bot_perms")],
            [("⏸ Pause", "pause") if record.enabled else ("▶️ Resume", "resume"), ("🗑 Destroy", "destroy")],
            [("⬅️ Back", "back"), ("✖️ Close", "cancel")],
        ]
        return Screen("\n".join(lines), rows)

    def _safe_record(self, session: Session) -> Optional[BotRecord]:
        try:
            return self._record(session)
        except BotError:
            session.notice = "⚠️ That bot no longer exists."
            session.screen = "list"
            return None

    def _screen_bot_model(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        typing = "\n\nSend the model's name as a message." if session.awaiting == "bot_model" else ""
        return Screen("Model for %s. It applies from its next message.%s" % (record.handle, typing),
                      self._model_rows(record.settings, self._backend(record.settings)) + [[("⬅️ Back", "back")]])

    def _screen_bot_effort(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        return Screen("Effort for %s." % record.handle,
                      self._effort_rows(record.settings, self._backend(record.settings)) + [[("⬅️ Back", "back")]])

    def _screen_bot_task(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        return Screen("Send the new task for %s as a message.\nNow: %s" % (record.handle, record.settings.get("task") or "none"),
                      [[("⬅️ Back", "back")]])

    def _screen_bot_perms(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        return Screen(
            "Permissions of %s\nNow: %s\n\nPick a preset, or Customize to change single permissions. "
            "A change applies from its next message.\n\n%s\n\n%s\n\n%s"
            % (record.handle, presets.level_name(record), self._preset_cards(), presets.card("cross"),
               bots.cross_agent_line(record)),
            self._preset_rows(record.permissions) + [self._cross_row(record), [("⬅️ Back", "back")]],
        )

    def _screen_bot_perms_custom(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        return Screen(
            "Permissions of %s, customized. Each tap applies from its next message.\nNow: %s\n\n%s"
            % (record.handle, presets.level_name(record), self._permission_text(record)),
            self._permission_rows(record, editing=True) + [[("⬅️ Back", "back")]],
        )

    def _screen_bot_perms_confirm(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        key = session.pending
        gained = presets.gains(record.permissions, presets.apply(key, record.permissions))
        return Screen(
            "Give %s more permissions?\n\nFrom: %s\nTo: %s\nIt gains: %s\n\n%s"
            % (record.handle, presets.name_of(record.permissions), presets.title(key), ", ".join(gained), presets.card(key)),
            [[("✅ Yes, switch to %s" % presets.PRESET_INFO[key].label, "pickok")], [("⬅️ No, back", "back")]],
        )

    def _screen_bot_cross(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        if record.cross_agent:
            return Screen(bots.cross_agent_line(record), [[("🔒 Withdraw it", "crossrevoke")], [("⬅️ Back", "back")]])
        text = bots.CROSS_AGENT_WARNING.format(name=record.name, handle=record.handle, username=record.username)
        return Screen(text, [[("⚠️ Yes, grant cross-agent access", "crossgrant")], [("⬅️ No, back", "back")]])

    def _screen_bot_destroy(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        scheduled = self.pool.store.count_for_bot(record.bot_id)
        return Screen(
            "⚠️ Destroy %s (%s)? This stops it, deletes its %d scheduled item(s), its conversations, memory and "
            "files, and revokes its token. It cannot be undone." % (record.name, record.handle, scheduled),
            [[("🗑 Yes, destroy it", "destroy!")], [("⬅️ No, back", "back")]],
        )
