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

from . import bots, presets, scheduler, shared_memory, soul, soul_writer, usage_alerts, usage_monitor
from .bots import BotError, BotRecord, DEFAULT_PERMISSIONS

SESSION_SECONDS = 30 * 60
CALLBACK_LIMIT = 64  # bytes, Telegram's limit for callback_data
PREFIX = "m"

Row = List[Tuple[str, str]]  # (label, action)

# What the model step offers. The empty value means "Consiglieri's default" (.env, or the backend's own).
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
    ("admin", "🧑‍💼 Create bots for you (bot admin)"),
    ("usage", "📊 Plan usage and alerts"),
    ("shared", "🧠 Write the shared memory"),
    ("soul", "🎭 Edit its own role and personality"),
)
SHOW_LIMIT = 3000  # characters of a personality shown on a screen (Telegram's message limit is 4096)

# The typed fields of a bot's "Folders and rules" screen: name -> (label, what to send).
ADVANCED_FIELDS = {
    "workdir": ("Working folder", "an absolute path, for example ~/projects/site"),
    "folders": ("Extra folders", "absolute paths separated by commas"),
    "allow": ("Allow rules", "Claude Code rules separated by ;, for example Bash(git status)"),
    "deny": ("Deny rules", "Claude Code rules separated by ;"),
}

WIZARD = ("new_name", "new_task", "new_soul", "new_model", "new_effort", "new_perms", "new_summary")
STEP_TITLES = {
    "new_name": "Step 1 of 7: name",
    "new_task": "Step 2 of 7: task",
    "new_soul": "Step 3 of 7: personality",
    "new_model": "Step 4 of 7: model",
    "new_effort": "Step 5 of 7: effort",
    "new_perms": "Step 6 of 7: permissions",
    "new_summary": "Step 7 of 7: check and create",
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
    soul: str = ""  # the draft's own personality; empty means the base
    # A personality Claude drafted from a description, waiting for Accept: nothing is saved before that.
    soul_target: str = ""  # "new" (the creation assistant) or "bot" (the bot being edited)
    soul_description: str = ""
    soul_preview: str = ""
    soul_warning: str = ""
    bot_id: int = 0  # the bot being edited
    preset: str = ""  # the preset the draft's permissions started from
    pending: str = ""  # a preset waiting for "yes" before it is applied to a bot
    shared_topics: List[str] = field(default_factory=list)  # the shared topics as last listed, for their buttons
    shared_topic: str = ""  # the shared topic on screen
    item_ids: List[str] = field(default_factory=list)  # the scheduled items as last listed, for their cancel buttons
    alert_ids: List[str] = field(default_factory=list)  # a bot's usage alerts as last listed, for their buttons
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


def _duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def _short(text: str, limit: int = 28) -> str:
    """A task on a button: one line, short enough for Telegram's button width."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _label_note(pool: Any, record: BotRecord) -> str:
    """For a bot whose SOUL.md has a role: the task is only its label, and what it does is changed there."""
    if pool is None or not soul.has_role(soul.read(pool.registry.workspace(record.bot_id))):
        return ""
    return (" Its role is in its personality (SOUL.md), so this changes only its label: to change what it does, "
            "use Personality.")


def _mark(on: bool) -> str:
    return "✅" if on else "⬜"


def permission_warnings(record: BotRecord, backend: str = "claude") -> List[str]:
    """Plain warnings for the risky choices, shown on the permissions and summary screens."""
    warnings = []
    if record.cross_agent:
        warnings.append("⚠️ CROSS-AGENT ACCESS: it reads the memory of your other bots and uses their tools.")
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
    if record.allows("shared"):
        warnings.append("⚠️ Shared memory: every other agent will read what it writes there, and that text could "
                        "carry instructions to them.")
    if record.allows("admin"):
        warnings.append("⚠️ Bot admin: it can send you requests for new bots and set up the ones you create, with up "
                        "to its own permissions. Anything more needs your separate yes; it can't destroy bots.")
    if record.allows("soul"):
        warnings.append("⚠️ Role and personality: it can rewrite its own SOUL.md, which now includes its mission and "
                        "scope, not only its tone, so it can change what it does and how. Never what it may do: that "
                        "stays its permissions. A text it reads could push it to change its own mission.")
    if record.permission("mode") == "auto":
        warnings.append("ℹ️ auto: a classifier model decides what runs without asking.")
    return warnings


class Menus:
    """Sessions and screens. `pool` is the BotPool; `request_bot` sends the creation request for a draft."""

    def __init__(
        self,
        pool: Any,
        request_bot: Callable[[int, int, str, Dict[str, str], Dict[str, str], str], Awaitable[str]],
        show: Optional[Callable[[Session, Screen], Awaitable[None]]] = None,
    ) -> None:
        self.pool = pool
        self.request_bot = request_bot
        self.show = show  # puts a screen up at once, for "writing…" while a draft is made
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
        except (BotError, shared_memory.SharedMemoryError, soul.SoulError, soul_writer.SoulWriterError) as exc:
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
                session.name, session.settings, session.soul = "", {}, ""
                session.preset, session.permissions = presets.RECOMMENDED, presets.apply(presets.RECOMMENDED, {})
            return self.go(session, argument)
        if verb == "skip":  # the task is optional
            session.settings.pop("task", None)
            return self.go(session, "new_soul")
        if verb == "soulbase":  # the personality step: the base, as it is
            session.soul = ""
            return self.go(session, "new_model")
        if verb == "soulwrite":
            session.awaiting = "soul"
            return self.render(session)
        if verb == "souldescribe":  # Claude drafts it from a description: first, the description
            if session.screen.startswith("bot_"):
                self._record(session)
            session.soul_target = "bot" if session.screen.startswith("bot_") else "new"
            session.awaiting = "soul_describe"
            return self.go(session, "bot_soul" if session.soul_target == "bot" else "new_soul")
        if verb == "souldefault":  # the task label as the description
            task = (self._record(session).settings.get("task", "") if session.screen.startswith("bot_")
                    else session.settings.get("task", ""))
            session.soul_target = "bot" if session.screen.startswith("bot_") else "new"
            if not task.strip():
                session.awaiting = "soul_describe"
                return self.render(session)
            return await self._draft(session, soul_writer.default_description(task))
        if verb == "soulaccept":
            if not session.soul_preview:
                return self.go(session, "new_soul" if session.soul_target != "bot" else "bot_soul")
            preview, target = session.soul_preview, session.soul_target
            self._drop_preview(session)
            if target == "bot":
                await self.pool.set_soul(self._record(session), preview)
                session.notice = "Personality saved. It applies from its next message, in the same conversation."
                return self.go(session, "bot_soul")
            session.soul = preview
            return self.go(session, "new_model")
        if verb == "soulagain":
            if not session.soul_description:
                return self.go(session, "new_soul" if session.soul_target != "bot" else "bot_soul")
            return await self._draft(session, session.soul_description)
        if verb == "souledit":  # by hand, starting from the draft the user sees
            draft, target = session.soul_preview, session.soul_target
            self._drop_preview(session)
            session.awaiting = "bot_soul" if target == "bot" else "soul"
            session.notice = "Send your version as a message. The draft, to copy and change:\n\n%s" % self._shown(draft)
            return self.go(session, "bot_soul" if target == "bot" else "new_soul")
        if verb == "soulcancel":
            target = session.soul_target
            self._drop_preview(session)
            session.notice = "Draft discarded; nothing was saved."
            return self.go(session, "bot_soul" if target == "bot" else "new_soul")
        if verb == "soulreplace":
            self._record(session)
            session.awaiting = "bot_soul"
            return self.go(session, "bot_soul")
        if verb == "soulreset":
            record = self._record(session)
            await self.pool.set_soul(record, None)
            session.notice = "Personality reset to the base. It applies from its next message, in the same conversation."
            return self.go(session, "bot_soul")
        if verb == "basereplace":
            session.awaiting = "base_soul"
            return self.go(session, "base_soul")
        if verb == "basereset":
            soul.reset_base(self.pool.config)
            session.notice = "Base personality reset to the template. Existing bots keep theirs."
            return self.go(session, "base_soul")
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
        if verb == "shtopic":
            topics = session.shared_topics
            session.shared_topic = topics[int(argument)] if argument.isdigit() and int(argument) < len(topics) else ""
            return self.go(session, "shared_topic" if session.shared_topic else "shared")
        if verb == "shadd":
            session.awaiting = "shared_add"
            return self.go(session, "shared_add")
        if verb == "shremove":
            return self.go(session, "shared_remove")
        if verb == "shremove!":
            removed = shared_memory.remove(self.pool.config, session.shared_topic)
            session.notice = "Removed %s:\n%s" % (session.shared_topic, removed.strip())
            session.shared_topic = ""
            return self.go(session, "shared")
        if verb == "create":
            if not session.name:
                session.notice = "⚠️ Give it a name first."
                return self.go(session, "new_name")
            username = await self.request_bot(session.user_id, session.chat_id, session.name, session.settings,
                                              session.permissions, session.soul)
            self.close(session)
            return Screen(
                "Request sent for %s (@%s). Tap \"Create @%s\" on the keyboard I just sent and confirm in "
                "Telegram; the link below it is the second option. I'll tell you here when it is ready."
                % (session.name, username, username)
            )
        if verb == "bot":
            # A button carries only the bot's id: whose bot it is is checked here, not trusted from the button.
            if not argument.isdigit() or self.pool.registry.get_owned(int(argument), session.user_id) is None:
                session.bot_id = 0
                session.notice = "⚠️ That bot is not one of yours."
                return self.go(session, "list")
            session.bot_id = int(argument)
            return self.go(session, "bot")
        if verb == "edittask":
            session.awaiting = "bot_task"
            return self.go(session, "bot_task")
        if verb == "advanced" and argument in ADVANCED_FIELDS:
            self._record(session)
            session.awaiting = "bot_%s" % argument
            return self.go(session, "bot_advanced")
        if verb == "unschedule":  # a reminder of the bot being edited, or of the old main bot from the home screen
            index = int(argument) if argument.isdigit() else -1
            item_id = session.item_ids[index] if 0 <= index < len(session.item_ids) else ""
            old = session.screen == "old_schedule"
            bot_id = 0 if old else self._record(session).bot_id
            if old:  # only the user's own chat's items from before the panel
                mine = {item["id"] for item in self.pool.store.pending_for_bot(0, chat_id=session.user_id)}
                item_id = item_id if item_id in mine else ""
            cancelled = self.pool.store.cancel_for_bot(item_id, bot_id) if item_id else None
            session.notice = "Cancelled: %s" % cancelled["message"][:80] if cancelled else "⚠️ That item is no longer pending."
            return self.render(session)
        if verb == "unalert":
            record = self._record(session)
            index = int(argument) if argument.isdigit() else -1
            alert_id = session.alert_ids[index] if 0 <= index < len(session.alert_ids) else ""
            removed = usage_alerts.AlertStore.for_bot(self.pool.config, record.bot_id).remove(alert_id) if alert_id else None
            session.notice = "Removed the alert %s." % removed.when if removed else "⚠️ That alert is already gone."
            return self.render(session)
        if verb == "shfact":
            number = int(argument) if argument.isdigit() else 0
            removed = shared_memory.remove(self.pool.config, session.shared_topic, number)
            session.notice = "Removed this fact:\n%s" % removed.strip()
            return self.go(session, "shared_topic")
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
                return self.go(session, "new_soul")
            if what == "soul":
                session.soul = soul.clean(text)
                return self.go(session, "new_model")
            if what == "soul_describe":
                if session.soul_target == "bot":
                    self._record(session)
                return await self._draft(session, text)
            if what == "bot_soul":
                await self.pool.set_soul(self._record(session), text)
                session.notice = "Personality replaced. It applies from its next message, in the same conversation."
                return self.go(session, "bot_soul")
            if what == "base_soul":
                soul.set_base(self.pool.config, text)
                session.notice = "Base personality replaced. New bots start with it; existing ones keep theirs."
                return self.go(session, "base_soul")
            if what == "model":
                session.settings["model"] = bots.clean_setting(self.pool.config, "model", text)
                return self.go(session, "new_effort")
            if what == "shared_add":
                topic, separator, fact = text.partition(":")
                if not separator:
                    raise shared_memory.SharedMemoryError("write it as topic: fact, for example user: lives in Madrid")
                shared_memory.add(self.pool.config, topic, fact)
                session.shared_topic = shared_memory.clean_topic(topic)
                session.notice = "Added. Every agent reads it from its next conversation."
                return self.go(session, "shared_topic")
            if what == "bot_task":
                await self.pool.set_setting(self._record(session), "task", text)
                record = self._record(session)
                session.notice = "Task changed. It applies from its next message." + _label_note(self.pool, record)
                return self.go(session, "bot")
            if what == "bot_model":
                await self.pool.set_setting(self._record(session), "model", text)
                session.notice = "Model changed. It applies from its next message."
                return self.go(session, "bot")
            if what and what[len("bot_"):] in ADVANCED_FIELDS:
                name = what[len("bot_"):]
                record = self._record(session)
                if name == "workdir":
                    await self.pool.set_setting(record, "workdir", text)
                else:
                    await self.pool.set_permission(record, name, text)
                session.notice = "%s changed. It applies from its next message." % ADVANCED_FIELDS[name][0]
                return self.go(session, "bot_advanced")
        except (BotError, shared_memory.SharedMemoryError, soul.SoulError, soul_writer.SoulWriterError) as exc:
            session.notice = "⚠️ %s" % exc
            session.awaiting = what
        return self.render(session)

    # -- personality drafts --

    @staticmethod
    def _drop_preview(session: Session) -> None:
        session.soul_preview = session.soul_description = session.soul_warning = ""

    async def _draft(self, session: Session, description: str) -> Screen:
        """One call to Claude for a personality draft, then the preview. Only the bot's owner gets here: for a
        bot, `_record` has checked it; in the assistant the draft is the user's own."""
        bot_id, name = 0, session.name
        if session.soul_target == "bot":
            record = self._record(session)
            bot_id, name = record.bot_id, record.name
        if self.show is not None:
            await self.show(session, Screen("✨ Writing a personality from your description (one call to Claude)…"))
        try:
            proposal = await self.pool.soul_writer.write(session.user_id, description, name, bot_id)
        except soul_writer.SoulWriterError as exc:
            session.notice = "⚠️ %s" % exc
            session.awaiting = "soul_describe" if not session.soul_preview else None
            return self.go(session, "soul_preview" if session.soul_preview else
                           ("bot_soul" if session.soul_target == "bot" else "new_soul"))
        session.soul_description, session.soul_preview, session.soul_warning = description, proposal.text, proposal.warning
        session.notice = "%d draft(s) left this hour." % proposal.attempts_left
        return self.go(session, "soul_preview")

    def _screen_soul_preview(self, session: Session) -> Screen:
        whose = session.name if session.soul_target != "bot" else (self._safe_record(session) or session.draft()).handle
        lines = ["Draft personality for %s, written by Claude from your description. Nothing is saved until you "
                 "tap Accept." % whose]
        if session.soul_warning:
            lines += ["", session.soul_warning]
        lines += ["", self._shown(session.soul_preview)]
        return Screen("\n".join(lines), [[("✅ Accept", "soulaccept"), ("🔁 Regenerate", "soulagain")],
                                          [("✏️ Edit by hand", "souledit"), ("✖️ Cancel", "soulcancel")]])

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
        if editing and changes.get("admin") == "on":  # becoming a bot admin always goes through the warning
            session.pending = "switch:admin"
            return self.go(session, "bot_perms_confirm")
        if editing:
            for key, value in changes.items():
                await self.pool.set_permission(record, key, value, by=session.user_id, via="menu")
        else:
            for key, value in changes.items():
                session.permissions[key] = bots.clean_permission(self.pool.config, key, value)
            session.permissions = {k: v for k, v in session.permissions.items() if v != DEFAULT_PERMISSIONS[k]}
        return self.render(session)

    async def _pick(self, session: Session, key: str, confirmed: bool = False) -> Screen:
        """A preset button. In the assistant it is chosen and the summary follows; on a bot it is applied,
        after a confirmation when it grants anything the bot does not have yet."""
        if key == "switch:admin" and session.screen.startswith("bot_"):
            session.pending = ""
            record = self._record(session)
            if confirmed:
                await self.pool.set_permission(record, "admin", "on", by=session.user_id, via="menu")
                session.notice = "%s is now a bot admin. It applies from its next message." % record.handle
            return self.go(session, "bot_perms_custom")
        if not presets.is_choice(key):
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
        await self.pool.set_permissions(record, target, by=session.user_id, via="menu")
        session.notice = "%s now has %s. It applies from its next message." % (record.handle, presets.title(key))
        return self.go(session, "bot_perms")

    def _record(self, session: Session) -> BotRecord:
        """The bot being edited, only if it is still there and belongs to whoever has this menu open."""
        record = self.pool.registry.get_owned(session.bot_id, session.user_id) if session.bot_id else None
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
        if screen in ("shared_topic", "shared_add"):
            return "shared"
        if screen in ("audit", "old_schedule", "base_soul"):
            return "home"
        if screen == "soul_preview":
            return "new_soul"
        if screen == "shared_remove":
            return "shared_topic"
        if screen.startswith("bot_"):
            return "bot"
        if screen == "bot":
            return "list"
        return "home"

    def go(self, session: Session, screen: str) -> Screen:
        session.screen = screen
        session.awaiting = {"new_name": "name", "new_task": "task", "bot_task": "bot_task",
                            "shared_add": "shared_add"}.get(screen, session.awaiting)
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
        rows: List[Row] = [[("➕ Create bot", "go:new_name")], [("🤖 My bots", "go:list")],
                           [("🧠 Shared memory", "go:shared"), ("📜 Audit", "go:audit")]]
        old = len(self.pool.store.pending_for_bot(0, chat_id=session.user_id)) if self.pool is not None else 0
        if old:  # items the main bot had scheduled in this chat before it became a panel
            rows.append([("⏰ Old reminders (%d)" % old, "go:old_schedule")])
        rows += [[("🎭 Base personality", "go:base_soul")], [("❓ Help", "go:help")], [("✖️ Close", "cancel")]]
        return Screen(
            "The Consiglieri control panel. There is no main agent: you create your own bots, and each has its own "
            "chat, task, model, permissions, memory and session.", rows)

    def _screen_help(self, session: Session) -> Screen:
        return Screen(
            "How it works\n\n"
            "Create bot walks you through name, task, model, effort and permissions, then sends a button "
            "that asks Telegram to create the bot. Confirm there and the bot is ready in its own chat.\n\n"
            "My bots lets you change a bot's model, effort, task, permissions, folders and rules, see and cancel "
            "its reminders and usage alerts, pause it, resume it or destroy it. You only ever see your own bots; "
            "there is no main agent.\n\n"
            "What a bot may do is a security preset: %s, %s, %s or %s, from least to most. Customize turns "
            "single permissions on and off; the bot then shows as Custom. Claude Code enforces the permissions; "
            "with a full shell and no sandbox a bot can do anything your user can.\n\n"
            "At every level a bot is kept out of other agents' memory and tools (your other bots'). "
            "Cross-agent access, above full access, lifts that: grant it in My bots → Permissions, after a "
            "warning. Every grant and withdrawal is logged (/bot audit).\n\n"
            "Shared memory holds general facts every agent reads (who you are, your preferences); bots only read "
            "it unless you let one write it under Customize. /shared does the same by text.\n\n"
            "Audit lists every grant and withdrawal of cross-agent access to your bots.\n\n"
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
            "%s\n\nSend in a line what %s is for, as a message. It is its label in lists, and the starting point "
            "of its role, which lives in its personality (next step).%s"
            % (STEP_TITLES["new_task"], session.name, current),
            rows + [self._nav()],
        )

    @staticmethod
    def _shown(text: str) -> str:
        text = text.strip()
        if len(text) <= SHOW_LIMIT:
            return text
        return text[:SHOW_LIMIT] + "\n… (%d characters in all)" % len(text)

    def _screen_new_soul(self, session: Session) -> Screen:
        if session.awaiting == "soul":
            return Screen("%s\n\nSend %s's personality as a message: tone, manner, how it talks. It shapes how it "
                          "talks, never what it may do (that is the permissions, next)." % (STEP_TITLES["new_soul"], session.name),
                          [self._nav()])
        if session.awaiting == "soul_describe":
            task = session.settings.get("task", "")
            rows: List[Row] = [[("✨ Use its task: %s" % _short(task), "souldefault")]] if task else []
            return Screen("%s\n\nDescribe %s in a sentence or two, as a message: what it is for, its scope and how "
                          "it should come across, for example: keeps my calendar tidy, nothing else; patient, a bit "
                          "of humour. Claude writes a draft of its role and personality (one small call) and you see "
                          "it before anything is saved." % (STEP_TITLES["new_soul"], session.name), rows + [self._nav()])
        if session.soul:
            current = "Now: its own personality (%d characters):\n\n%s" % (len(session.soul), self._shown(session.soul))
            rows: List[Row] = [[("Keep it ➡️", "next")], [("↩️ Use the base instead", "soulbase")],
                               [("✍️ Write another", "soulwrite"), ("✨ Describe it", "souldescribe")]]
        else:
            current = "By default it starts with the base personality:\n\n%s" % self._shown(soul.base(self.pool.config))
            rows = [[("Use the base ➡️", "soulbase")], [("✍️ Write its own", "soulwrite")],
                    [("✨ Describe it, Claude writes it", "souldescribe")]]
        return Screen("%s\n\nHow should %s come across? Optional.\n\n%s" % (STEP_TITLES["new_soul"], session.name, current),
                      rows + [self._nav()])

    def _screen_bot_soul(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        current = soul.read(self.pool.registry.workspace(record.bot_id)) or soul.base(self.pool.config)
        lines = ["Personality of %s (its SOUL.md). It shapes how it talks, never what it may do: that is its "
                 "permissions." % record.handle, "", self._shown(current)]
        if session.awaiting == "bot_soul":
            lines += ["", "Send the new personality as a message. Nothing changes until you send it."]
        if session.awaiting == "soul_describe":
            lines += ["", "Describe what it is for and how it should come across, in a sentence or two, as a message. "
                      "Claude writes a draft of its role and personality (one small call) and you see it before "
                      "anything is saved."]
        if record.allows("soul"):
            lines += ["", "⚠️ It may edit its own SOUL.md, its role and mission included (Permissions → Customize)."]
        rows: List[Row] = [[("✏️ Replace", "soulreplace"), ("✨ Describe it", "souldescribe")]]
        if session.awaiting == "soul_describe" and record.settings.get("task"):
            rows.append([("✨ Use its task: %s" % _short(record.settings["task"]), "souldefault")])
        rows += [[("↩️ Reset to the base", "soulreset")], [("⬅️ Back", "back")]]
        return Screen("\n".join(lines), rows)

    def _screen_base_soul(self, session: Session) -> Screen:
        config = self.pool.config
        state = "edited" if soul.base_is_edited(config) else "the template, not edited yet"
        lines = ["Base personality (%s): what every new bot starts with, unless you write its own when you "
                 "create it. Existing bots keep theirs." % state, "", self._shown(soul.base(config))]
        if session.awaiting == "base_soul":
            lines += ["", "Send the new base personality as a message."]
        return Screen("\n".join(lines), [[("✏️ Replace", "basereplace")], [("↩️ Reset to the template", "basereset")],
                                          [("⬅️ Back", "back")]])

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
            "%s\n\nWhich model should %s use? Default means Consiglieri's setting.%s"
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
                           for key in presets.choices()]
        rows.append([("%s Customize" % presets.CUSTOM_ICON, "custom")])
        return rows

    @staticmethod
    def _preset_cards() -> str:
        return "\n\n".join(presets.card(key) for key in presets.choices())

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
        lines.append("Personality: %s" % ("its own (%d characters)" % len(session.soul) if session.soul else "the base"))
        lines += ["Permissions: %s" % presets.name_of(session.permissions, session.preset), "", self._permission_text(draft)]
        return Screen("\n".join(lines), [[("✅ Create", "create")], self._nav()])

    def _screen_list(self, session: Session) -> Screen:
        records = self.pool.registry.list(owner_id=session.user_id)  # only yours
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
            [("🎭 Personality", "go:bot_soul")],
            [("⏰ Reminders", "go:bot_schedule"), ("📊 Usage alerts", "go:bot_alerts")],
            [("📂 Folders and rules", "go:bot_advanced")],
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
        return Screen("Send the new task (its short label) for %s as a message.%s\nNow: %s"
                      % (record.handle, _label_note(self.pool, record), record.settings.get("task") or "none"),
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
        if key == "switch:admin":
            return Screen(bots.ADMIN_WARNING.format(handle=record.handle),
                          [[("✅ Yes, make it a bot admin", "pickok")], [("⬅️ No, back", "back")]])
        if not presets.is_choice(key):
            return self._screen_bot_perms(session)
        target = presets.apply(key, record.permissions)
        gained = presets.gains(record.permissions, target)
        admin = "\n\n" + bots.ADMIN_WARNING.format(handle=record.handle) if target.get("admin") == "on" and not record.allows("admin") else ""
        return Screen(
            "Give %s more permissions?\n\nFrom: %s\nTo: %s\nIt gains: %s\n\n%s%s"
            % (record.handle, presets.name_of(record.permissions), presets.title(key), ", ".join(gained), presets.card(key), admin),
            [[("✅ Yes, switch to %s" % presets.info(key).label, "pickok")], [("⬅️ No, back", "back")]],
        )

    def _screen_bot_cross(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        if record.cross_agent:
            return Screen(bots.cross_agent_line(record), [[("🔒 Withdraw it", "crossrevoke")], [("⬅️ Back", "back")]])
        text = bots.CROSS_AGENT_WARNING.format(name=record.name, handle=record.handle, username=record.username)
        return Screen(text, [[("⚠️ Yes, grant cross-agent access", "crossgrant")], [("⬅️ No, back", "back")]])

    # -- the shared memory --

    def _screen_shared(self, session: Session) -> Screen:
        config = self.pool.config
        found = shared_memory.entries(config)
        session.shared_topics = [topic for topic, _ in found]
        lines = ["Shared memory: general facts all your bots read. It is context for "
                 "them, never instructions; never put passwords, tokens or keys in it. Bots only read it unless "
                 "you let one write (Permissions → Customize).", "", "Folder: %s" % shared_memory.directory(config)]
        lines += [""] + (["• %s: %s" % entry for entry in found] if found else ["It is empty."])
        rows: List[Row] = [[("📄 %s" % topic, "shtopic:%d" % index)] for index, topic in enumerate(session.shared_topics[:20])]
        rows += [[("➕ Add a fact", "shadd")], [("⬅️ Back", "back"), ("✖️ Close", "cancel")]]
        return Screen("\n".join(lines), rows)

    def _screen_shared_topic(self, session: Session) -> Screen:
        try:
            text = shared_memory.showing(self.pool.config, session.shared_topic)
        except shared_memory.SharedMemoryError as exc:
            session.notice = "⚠️ %s" % exc
            return self._screen_shared(session)
        count = len(shared_memory.facts(self.pool.config, session.shared_topic))
        buttons = [("🗑 %d" % number, "shfact:%d" % number) for number in range(1, min(count, 20) + 1)]
        rows: List[Row] = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        hint = "\n\nTap a number to remove that fact." if buttons else ""
        return Screen(text + hint, rows + [[("➕ Add a fact", "shadd"), ("🗑 Remove topic", "shremove")], [("⬅️ Back", "back")]])

    def _screen_shared_add(self, session: Session) -> Screen:
        hint = " to %s" % session.shared_topic if session.shared_topic else ""
        example = session.shared_topic or "user"
        return Screen("Send the fact%s as a message, as topic: fact. For example:\n%s: lives in Madrid, works as a "
                      "designer\n\nEvery agent will read it. No passwords, tokens or keys." % (hint, example),
                      [[("⬅️ Back", "back")]])

    def _screen_shared_remove(self, session: Session) -> Screen:
        return Screen("Remove the shared topic %s and all its facts? Every agent stops seeing it from its next "
                      "conversation." % session.shared_topic,
                      [[("🗑 Yes, remove it", "shremove!")], [("⬅️ No, back", "back")]])

    # -- reminders, usage alerts, folders and rules, the audit log --

    def _item_lines(self, session: Session, items: List[Dict[str, Any]]) -> Tuple[List[str], List[Row]]:
        session.item_ids = [item["id"] for item in items[:20]]
        lines, buttons = [], []
        for number, item in enumerate(items[:20], 1):
            due = scheduler.parse_time(item["due_at_utc"]).astimezone().strftime("%Y-%m-%d %H:%M")
            repeat = " (every %s)" % _duration(item["repeat_seconds"]) if item["repeat_seconds"] else ""
            lines.append("%d. %s · %s%s · %s" % (number, due, item["kind"], repeat, " ".join(item["message"].split())[:100]))
            buttons.append(("🗑 %d" % number, "unschedule:%d" % (number - 1)))
        return lines, [buttons[i:i + 5] for i in range(0, len(buttons), 5)]

    def _screen_bot_schedule(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        items = self.pool.store.pending_for_bot(record.bot_id)
        lines, rows = self._item_lines(session, items)
        head = ("Reminders and timed tasks of %s, soonest first. It schedules them itself (alfred schedule, or when "
                "you ask it in its chat); tap a number to cancel one." % record.handle)
        if not record.allows("schedule"):
            head += "\n\nIts Reminders permission is off, so it can't add new ones."
        return Screen("\n".join([head, ""] + (lines or ["Nothing pending."])), rows + [[("⬅️ Back", "back")]])

    def _screen_old_schedule(self, session: Session) -> Screen:
        items = self.pool.store.pending_for_bot(0, chat_id=session.user_id)
        lines, rows = self._item_lines(session, items)
        head = ("Reminders the main bot scheduled in this chat before it became the control panel. There is no main "
                "agent any more: a reminder message is still posted here when it is due, and a task is announced, "
                "not carried out (give it to one of your bots). Tap a number to cancel one.")
        return Screen("\n".join([head, ""] + (lines or ["Nothing pending."])), rows + [[("⬅️ Back", "back")]])

    def _screen_bot_alerts(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        config = self.pool.config
        alerts = usage_alerts.AlertStore.for_bot(config, record.bot_id).alerts()
        session.alert_ids = [alert.id for alert in alerts[:20]]
        lines = ["Usage alerts of %s: conditions on the Claude plan usage that wake it. It sets them itself "
                 "(alfred usage-alert) when it has the Plan usage permission; each wake-up uses plan usage." % record.handle, ""]
        if not record.allows("usage"):
            lines.append("Its Plan usage permission is off (Permissions → Customize), so it can't add alerts.")
        note = usage_monitor.permission_note(config, record)
        if note:
            lines.append(note)
        if not usage_monitor.is_enabled(config):
            lines.append("The usage monitor is off (%s=0 in .env), so no alert is checked." % usage_monitor.ENABLE_SETTING)
        lines.append("")
        for number, alert in enumerate(alerts[:20], 1):
            state = "fired" if alert.spent else ("repeats" if alert.repeat else "once")
            lines.append("%d. when %s · %s%s" % (number, alert.when, state, " · %s" % alert.note[:80] if alert.note else ""))
        if not alerts:
            lines.append("No alerts.")
        buttons = [("🗑 %d" % number, "unalert:%d" % (number - 1)) for number in range(1, len(session.alert_ids) + 1)]
        rows: List[Row] = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        return Screen("\n".join(lines), rows + [[("⬅️ Back", "back")]])

    def _screen_bot_advanced(self, session: Session) -> Screen:
        record = self._safe_record(session)
        if record is None:
            return self._screen_list(session)
        lines = ["Folders and rules of %s. Each change applies from its next message." % record.handle, ""]
        for name, (label, hint) in ADVANCED_FIELDS.items():
            value = record.settings.get(name) if name == "workdir" else record.permission(name)
            lines.append("%s: %s" % (label, value or "none"))
        awaiting = (session.awaiting or "")[len("bot_"):]
        if awaiting in ADVANCED_FIELDS:
            label, hint = ADVANCED_FIELDS[awaiting]
            lines += ["", "Send the new %s as a message (%s), or default to clear it." % (label.lower(), hint)]
        rows: List[Row] = [[("✏️ %s" % label, "advanced:%s" % name)] for name, (label, _) in ADVANCED_FIELDS.items()]
        return Screen("\n".join(lines), rows + [[("⬅️ Back", "back")]])

    def _screen_audit(self, session: Session) -> Screen:
        return Screen(bots.audit_report(self.pool.config, session.user_id), [[("⬅️ Back", "back")]])

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
