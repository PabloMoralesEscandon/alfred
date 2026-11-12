"""The Telegram front end: authorization, commands, attachments, progress, and scheduled deliveries.

Receiving and replying are separate. A message is handed to the agent and the
handler returns; the agent's results arrive later through the `Listener`
methods, whenever a turn ends, including turns the agent started by itself.

One process serves several bots. `BotPool` runs the control panel (the main bot,
TELEGRAM_BOT_TOKEN), which has no agent and no Claude session, and every bot its
users created (see `alfred/bots.py`), each as its own python-telegram-bot
`Application` with its own `TelegramBot` front end and its own `Agent`, all in one
event loop with one scheduler. The panel's Python code does all the managing:
creating, configuring and destroying bots, the scheduler and its deliveries, the
shared memory, the usage monitor and the audit log. A bot created while the
process runs is started on the spot. Nothing is shared between bots except the
scheduler's table, where every item names its bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
    ReplyKeyboardRemove, Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler,
    filters,
)

from . import bots, home, menus, orca, presets, shared_memory, soul, soul_writer, usage, usage_monitor
from .agent import Agent, Chat, Listener
from .backends import BACKENDS, BackendError, RunResult
from .bots import BotError, BotRecord, BotRegistry
from .config import Config
from .scheduler import KIND_EVENT, KIND_TASK, Item, Postpone, SchedulerStore, parse_time, run_scheduler
from .text import split_message

log = logging.getLogger("alfred.telegram")

# The commands of a bot (each one has an agent), in the order /help and the menu show them.
# To add one: add a line here and a `_cmd_<name>` method on TelegramBot.
COMMANDS = [
    ("help", "Show what this bot can do and the commands"),
    ("new", "Start a fresh conversation (memory and skills are kept)"),
    ("stop", "Stop what the bot is doing right now"),
    ("usage", "How much of the Claude plan limits is used"),
    ("status", "Backend, session, browser and directory"),
    ("memory", "List what the bot has memorized"),
    ("skills", "List the skills the bot has"),
    ("schedule", "List pending reminders and tasks"),
    ("backend", "Show or switch the agent backend: claude or codex"),
]
# The control panel's commands: the main bot has no agent; it creates and manages the bots.
PANEL_COMMANDS = [
    ("help", "What this control panel does"),
    ("bots", "List your bots"),
    ("bot", "Menu to create and manage bots; /bot help for the text shortcuts"),
    ("status", "Whether bot management is on, and how many bots run"),
    ("usage", "How much of the Claude plan limits is used"),
    ("shared", "The shared memory every agent reads: list, show, add, remove"),
]

PROGRESS_INTERVAL = 2.5
TYPING_INTERVAL = 4.0
# The agent's whole final message when it has nothing to add to what it already sent.
NO_REPLY = "NO_REPLY"
UNSAFE_FILENAME = re.compile(r"[^\w.\-]+")
ATTACHMENT_KINDS = (("document", ""), ("voice", ".ogg"), ("audio", ".mp3"), ("video", ".mp4"), ("video_note", ".mp4"))
# What each kind of bot asks Telegram for. `managed_bot` is how the main bot hears about a new bot.
MAIN_UPDATES = ["message", "callback_query", "managed_bot"]
BOT_UPDATES = ["message", "callback_query"]
CONFIRM_SECONDS = 300
CANCEL_CREATION = "Cancel"  # the second key of the creation keyboard
BOT_USAGE = """I am the Consiglieri control panel. There is no main agent: I create and run your bots, each with its own chat, task, model, permissions, memory, tools and Claude session. You see and manage only your own.

/bot - the menu, where everything is managed with buttons: create a bot step by step; in My bots change its settings, permissions, folders and rules, see and cancel its reminders and usage alerts, pause, resume or destroy it; the shared memory; the audit log

Text shortcuts:
/bots - list them
/bot new <name> [options] [task] - create one (Telegram asks you to confirm)
/bot show <bot> - its settings and permissions
/bot set <bot> <setting> <value> - change a setting; `default` clears it
/bot perm <bot> <permission> <value> - change a permission
/bot perm <bot> <preset> - switch to a preset: %s
/bot perm <bot> cross on|off - cross-agent access: read other agents' memory and use their tools (asks first)
/bot audit - the last grants and withdrawals of cross-agent access
/shared - the shared memory every agent reads (list, show, add, remove); bots only read it
/bot pause <bot>, /bot resume <bot> - disconnect or reconnect it
/bot destroy <bot> - delete it, its schedule and its state (asks first)
/bot soul <bot> show|set <text>|reset - its personality (SOUL.md); reset puts the base back
/bot base-soul show|set <text>|reset - the base personality new bots start with

Settings: %s.
Permissions: %s.
Options for /bot new: +web +browser +shell +edit +mcp +contact +bots +usage +bypass, -schedule, -sandbox, and setting=value or permission=value (for example model=sonnet folders=~/notes).
A new bot starts with the least: its own directory, the alfred command, reminders, nothing else.
At every level, even full access, a bot can't read the memory of your other bots or use their tools. Only cross-agent access lifts that, and only you can grant it here.
<bot> is its @username, its name or its id.""" % (", ".join(presets.keys()), ", ".join(bots.SETTING_NAMES), ", ".join(bots.PERMISSION_NAMES))


class Progress:
    """A typing indicator plus one status message that is edited as the agent uses tools."""

    def __init__(self, bot: Any, chat_id: int, show_tools: bool) -> None:
        self.bot = bot
        self.chat = Chat(chat_id)
        self.show_tools = show_tools
        self._message: Optional[Message] = None
        self._last_edit = float("-inf")  # monotonic() can be near 0 right after start
        self._typing: Optional["asyncio.Future[None]"] = None

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id=int(self.chat), action=ChatAction.TYPING, **self.chat.send_kwargs())
            except Exception:
                pass
            await asyncio.sleep(TYPING_INTERVAL)

    def start(self) -> None:
        self._typing = asyncio.ensure_future(self._keep_typing())

    async def update(self, note: str) -> None:
        if not self.show_tools or time.monotonic() - self._last_edit < PROGRESS_INTERVAL:
            return
        self._last_edit = time.monotonic()
        text = "⏳ %s" % note
        try:
            if self._message is None:
                self._message = await self.bot.send_message(
                    chat_id=int(self.chat), text=text, disable_notification=True, **self.chat.send_kwargs()
                )
            else:
                await self._message.edit_text(text)
        except Exception:
            pass  # progress is cosmetic; never let it break a turn

    async def finish(self) -> None:
        if self._typing is not None:
            self._typing.cancel()
        if self._message is not None:
            try:
                await self._message.delete()
            except Exception:
                pass


def is_silent(text: str) -> bool:
    """True when the agent's final message says there is nothing to post."""
    return text.strip().strip("`*_.\"' ").upper() in ("", NO_REPLY)


def chat_of(update: Update) -> Chat:
    """The conversation an update belongs to: its chat, and its topic when it was sent in one."""
    message = update.effective_message
    thread = getattr(message, "message_thread_id", None) if getattr(message, "is_topic_message", False) else None
    return Chat(update.effective_chat.id, thread)


def command_text(message: Any) -> str:
    """The text after `/command` (or `/command@bot`), with its line breaks kept."""
    text = (getattr(message, "text", None) or "").strip()
    return text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""


class TelegramBot(Listener):
    """One bot's front end: its chats, its commands and its agent. Every one belongs to a registered bot;
    the control panel is `ControlPanel`, which has no agent."""

    def __init__(
        self,
        config: Config,
        agent: Optional[Agent] = None,
        *,
        record: BotRecord,
        pool: Optional["BotPool"] = None,
    ) -> None:
        if record is None or not config.bot_id:
            raise ValueError("a TelegramBot is one of the bots; the control panel is ControlPanel")
        self.config = config
        self.record = record
        self.pool = pool
        self.agent = agent or Agent(config)
        self.agent.listener = self
        self.agent.bot_username = record.username
        self.store = SchedulerStore(config.db_path)
        self.bot: Any = None  # set when the application starts, or by the first update
        self._progress: Dict[int, Progress] = {}

    @property
    def bot_id(self) -> int:
        return self.config.bot_id

    @property
    def commands(self) -> List[Tuple[str, str]]:
        return COMMANDS

    @property
    def title(self) -> str:
        return self.record.handle

    # -- wiring -------------------------------------------------------------

    def build(self, token: Optional[str] = None) -> Application:
        if not token:
            raise ValueError("a bot connects with its own token, never the panel's")
        application = (
            ApplicationBuilder()
            .token(token)
            .concurrent_updates(True)  # so /stop is handled while a turn is running
            .build()
        )
        for name in ["start"] + [name for name, _ in self.commands]:
            application.add_handler(CommandHandler(name, getattr(self, "_cmd_%s" % name)))
        # Everything else, including unknown /commands (which invoke a skill by name), goes to the agent.
        application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL & ~filters.UpdateType.EDITED, self._on_message))
        application.add_error_handler(self._on_error)
        return application

    async def started(self, application: Application) -> None:
        """The application is connected: publish the command menu and remember the bot object."""
        self.bot = application.bot
        try:
            await application.bot.set_my_commands([BotCommand(name, description) for name, description in self.commands])
        except Exception as exc:  # the menu is a convenience; a bot without one still works
            log.warning("could not set the command menu of %s: %s", self.title, exc)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("update failed in %s", self.title, exc_info=context.error)

    # -- helpers ------------------------------------------------------------

    def _authorized(self, update: Update) -> bool:
        """The allow-list, and for a bot also its owner: an allowed user never uses someone else's bot.
        Shared by the panel, whose `record` is None (there every allowed user is served, each with their own bots)."""
        user = update.effective_user
        if user is None or update.effective_chat is None or update.effective_message is None:
            return False
        record = getattr(self, "record", None)
        if record is not None and int(user.id) != int(record.owner_id):
            log.warning("ignored message from user id=%s to %s, which belongs to someone else", user.id, self.title)
            return False
        if self.config.is_allowed(user.id, user.username):
            # No message content in the log; just enough to see that an update arrived.
            text = getattr(update.effective_message, "text", None) or ""
            kind = text.split()[0] if text.startswith("/") else "message"
            log.info("%s from user id=%s in chat %s of %s", kind, user.id, update.effective_chat.id, self.title)
            return True
        log.warning("ignored message from unauthorized user id=%s username=%s to %s", user.id, user.username, self.title)
        return False

    @staticmethod
    async def _send(bot: Any, chat_id: int, text: str, **extra: Any) -> None:
        chat = Chat(chat_id)
        for chunk in split_message(text) or ["(empty reply)"]:
            await bot.send_message(chat_id=int(chat), text=chunk, **chat.send_kwargs(), **extra)

    async def _save_attachments(self, message: Message) -> List[str]:
        """Download whatever was attached into the inbox and return prompt notes with the paths."""
        candidates: List[Any] = []  # (attachment, extension to use when Telegram gives no file name)
        if message.photo:
            candidates.append((message.photo[-1], ".jpg"))  # the largest size
        for kind, extension in ATTACHMENT_KINDS:
            attachment = getattr(message, kind, None)
            if attachment is not None:
                candidates.append((attachment, extension))

        notes: List[str] = []
        folder = self.config.inbox_dir / str(message.chat_id)
        for attachment, extension in candidates:
            original = Path(getattr(attachment, "file_name", None) or "")
            label = UNSAFE_FILENAME.sub("_", original.stem)[:60] or "file"
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = folder / ("%s-%s%s" % (stamp, label, original.suffix or extension))
            try:
                folder.mkdir(parents=True, exist_ok=True)
                telegram_file = await attachment.get_file()
                await telegram_file.download_to_drive(custom_path=str(target))
            except Exception as exc:
                notes.append("[The user attached a file, but it could not be downloaded: %s]" % exc)
            else:
                notes.append("[The user attached a file, saved at %s]" % target)
        return notes

    # -- what the agent reports (Listener) -----------------------------------

    async def turn_started(self, chat_id: int) -> None:
        if chat_id not in self._progress and self.bot is not None:
            progress = self._progress[chat_id] = Progress(self.bot, chat_id, self.config.show_progress)
            progress.start()

    async def progress(self, chat_id: int, note: str) -> None:
        progress = self._progress.get(chat_id)
        if progress is not None:
            await progress.update(note)

    async def _clear_progress(self, chat_id: int) -> None:
        progress = self._progress.pop(chat_id, None)
        if progress is not None:
            await progress.finish()

    async def result(self, chat_id: int, result: RunResult) -> None:
        """Post what a turn ended with: its final message, unless it is NO_REPLY, plus notes on what changed."""
        await self._clear_progress(chat_id)
        parts: List[str] = []
        if result.is_error:
            parts.append("⚠️ %s" % result.text)
        elif not is_silent(result.text):
            parts.append(result.text)
        if result.session_restarted:
            parts.append("(I couldn't reopen our earlier conversation, so this one starts fresh. My memory and skills are intact.)")
        if result.memory_changes:
            parts.append("💾 Memory: %s" % ", ".join(result.memory_changes))
        if result.new_skills:
            parts.append("🧠 New skill: %s" % ", ".join(result.new_skills))
        for change in result.extension_changes:
            parts.append("🧩 Extension %s" % change)
        # No content in the log; just enough to see that a turn ended and whether anything was posted.
        log.info("turn ended in chat %r of %s: %s", chat_id, self.title,
                 "error" if result.is_error else "posted" if parts else "nothing to post")
        if parts and self.bot is not None:
            await self._send(self.bot, chat_id, "\n\n".join(parts))

    async def _deliver(self, item: Item) -> None:
        """A scheduled item came due: post a fixed message, or wake the agent with a task or an event."""
        chat = Chat(item["chat_id"], item.get("thread_id"))
        if item["kind"] in (KIND_TASK, KIND_EVENT):
            label, source = ("Scheduled task", "schedule") if item["kind"] == KIND_TASK else ("Event", "event")
            await self.agent.submit(
                chat,
                "[%s] %s" % (label, item["message"]),
                user_id=item["user_id"] or None,
                username=item["username"] or None,
                source=source,
            )
        elif self.bot is not None:
            await self._send(self.bot, chat, "⏰ %s" % item["message"])

    # -- handlers -----------------------------------------------------------

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        parts = [message.text or message.caption or ""]
        parts += await self._save_attachments(message)
        text = "\n\n".join(part for part in parts if part.strip())
        if not text:
            return
        # Returns as soon as the agent has the message. If it is mid-turn, it reads this at its next step.
        await self.agent.submit(
            chat_of(update),
            text,
            user_id=update.effective_user.id,
            username=update.effective_user.username,
        )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is not None and update.effective_message is not None and not self.config.is_allowed(user.id, user.username):
            # The id is what the owner needs to put in ALFRED_ALLOWED_USERS.
            await update.effective_message.reply_text("This is a private bot. Your Telegram user id is %s." % user.id)
            return
        if user is not None and update.effective_message is not None and int(user.id) != int(self.record.owner_id):
            await update.effective_message.reply_text("This bot belongs to someone else. Create your own from the control panel.")
            return
        if self.pool is not None:
            self.pool.link_chat(self.record.bot_id, update.effective_chat.id)
        await self._cmd_help(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        task = self.record.settings.get("task")
        lines = ["I'm %s, a Consiglieri bot made for one job." % self.record.name]
        lines.append("My task: %s" % task if task else "I have no task yet; give me one from the Consiglieri control panel (/bot set).")
        lines.append("Write to me in plain language. My memory and files are my own.")
        lines += ["", "Commands:"]
        lines += ["/%s - %s" % command for command in self.commands]
        lines += ["", "Any other /name is passed to me as a request, so /disk-report asks for that skill."]
        await update.effective_message.reply_text("\n".join(lines))

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        await update.effective_message.reply_text(await usage.fetch(self.config, dict(os.environ)))

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        chat = chat_of(update)
        await self.agent.new_conversation(chat)
        await self._clear_progress(chat)
        await update.effective_message.reply_text("Fresh conversation. I still have my memory and skills.")

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        chat = chat_of(update)
        was_working = await self.agent.stop(chat)
        await self._clear_progress(chat)
        await update.effective_message.reply_text("Stopped." if was_working else "Nothing is running.")

    async def _cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        memories = home.list_memories(self.config)
        if not memories:
            await update.effective_message.reply_text(
                "Nothing memorized yet. Tell me to remember something, or I will note things as we go."
            )
            return
        lines = ["%d memory file(s). Ask me about any of them, or tell me to forget one." % len(memories)]
        lines += ["\n• %s\n%s" % (memory.key, memory.description or "(no description)") for memory in memories]
        await self._send(context.bot, chat_of(update), "\n".join(lines))

    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        skills = home.list_skills(self.config)
        if not skills:
            await update.effective_message.reply_text("No skills yet.")
            return
        lines = ["%d skill(s):" % len(skills)]
        lines += ["\n• %s\n%s" % (skill.name, skill.description) for skill in skills]
        await self._send(context.bot, chat_of(update), "\n".join(lines))

    async def _cmd_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        items = self.store.list(update.effective_chat.id, bot_id=self.bot_id)
        if not items:
            await update.effective_message.reply_text("Nothing scheduled.")
            return
        lines = []
        for item in items:
            when = parse_time(item["due_at_utc"]).astimezone().strftime("%a %d %b %H:%M")
            repeat = " (repeats)" if item["repeat_seconds"] else ""
            lines.append("%s  %s%s  [%s]\n%s" % (item["id"][:8], when, repeat, item["kind"], item["message"]))
        await self._send(context.bot, chat_of(update), "\n\n".join(lines))

    async def _cmd_backend(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        chat = chat_of(update)
        if not context.args:
            await update.effective_message.reply_text(
                "Backend: %s. Switch with /backend %s." % (self.agent.backend_name(chat), " | ".join(sorted(BACKENDS)))
            )
            return
        try:
            await self.agent.switch_backend(chat, context.args[0].lower())
        except BackendError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await self._clear_progress(chat)
        await update.effective_message.reply_text(
            "Now using %s for this chat. Each backend keeps its own conversation; memory and skills are shared."
            % self.agent.backend_name(chat)
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        chat = chat_of(update)
        backend = self.agent.backend_name(chat)
        pid = self.agent.live_pid(chat)
        workdir = self.config.workdir
        lines = ["Bot: %s (%s)" % (self.record.name, self.record.handle),
                 "Works in: %s" % (workdir or "its own directory")]
        lines += [
            "Backend: %s" % backend,
            "Model: %s" % self.agent.model(chat),
            "Effort: %s" % self.agent.effort(chat),
            "Session: %s" % (self.agent.sessions.session(chat, backend) or "none yet"),
            "Busy: %s" % ("yes" if self.agent.is_busy(chat) else "no"),
            "Process: %s" % (("open, pid %s" % pid) if pid else "not running; it starts with your next message"),
            "Memories: %d" % len(home.list_memories(self.config)),
            "Skills: %d" % len(home.list_skills(self.config)),
            "Scheduled: %d" % len(self.store.list(chat, bot_id=self.bot_id)),
            "Browser: %s" % await asyncio.get_event_loop().run_in_executor(None, orca.summary, self.config),
            "Home: %s" % self.config.workspace,
        ]
        await update.effective_message.reply_text("\n".join(lines))


class ControlPanel:
    """The main bot (TELEGRAM_BOT_TOKEN). It has no agent and no Claude session: it creates, lists,
    configures and destroys the bots that do the work, and is the manager of their Telegram accounts."""

    title = "control panel"
    record = None

    def __init__(self, config: Config, pool: Optional["BotPool"] = None) -> None:
        self.config = config
        self.pool = pool
        self.bot: Any = None
        self.store = SchedulerStore(config.db_path)
        # The /bot menus: sessions in memory only, so a restart closes them all (their buttons then say so).
        self.menus = menus.Menus(pool, self.send_creation_request, show=self._show) if pool is not None else None

    @property
    def commands(self) -> List[Tuple[str, str]]:
        return PANEL_COMMANDS

    def build(self, token: Optional[str] = None) -> Application:
        application = (
            ApplicationBuilder()
            .token(token or self.config.telegram_token)
            .concurrent_updates(True)
            .build()
        )
        for name in ["start"] + [name for name, _ in self.commands]:
            application.add_handler(CommandHandler(name, getattr(self, "_cmd_%s" % name)))
        application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL & ~filters.UpdateType.EDITED, self._on_message))
        application.add_handler(CallbackQueryHandler(self._on_button), group=1)
        # Every update passes here first; only `managed_bot` ones are acted on.
        application.add_handler(TypeHandler(Update, self._on_managed_bot), group=-1)
        application.add_error_handler(self._on_error)
        return application

    async def started(self, application: Application) -> None:
        self.bot = application.bot
        try:
            await application.bot.set_my_commands([BotCommand(name, description) for name, description in self.commands])
        except Exception as exc:
            log.warning("could not set the control panel's command menu: %s", exc)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("update failed in the control panel", exc_info=context.error)

    _authorized = TelegramBot._authorized
    _send = staticmethod(TelegramBot._send)

    async def _deliver(self, item: Item) -> None:
        """An item scheduled for the main bot from before it became a panel (bot 0; nothing new is scheduled
        there). A reminder message is still posted; a task or an event is announced, never run, because the
        panel has no agent. Nothing is deleted: see docs/PANEL_ONLY.md."""
        chat = Chat(item["chat_id"], item.get("thread_id"))
        if self.bot is None:
            raise RuntimeError("the control panel is not connected")
        if item["kind"] in (KIND_TASK, KIND_EVENT):
            await self._send(self.bot, chat, "⏰ This task was scheduled for the old main agent. There is no main agent any more, "
                                             "so I can't carry it out. Give it to one of your bots: %s" % item["message"])
        else:
            await self._send(self.bot, chat, "⏰ %s" % item["message"])

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if bots.managed_bot_created(message) is not None:
            return  # "you created a bot": the managed_bot update that comes with it does the work
        text = (getattr(message, "text", None) or "").strip()
        if not text:
            return
        session = self.menus.waiting_for_text(update.effective_user.id) if self.menus is not None else None
        if session is not None and text != CANCEL_CREATION:
            await self._show(session, await self.menus.text(session, text))
            return
        if text == CANCEL_CREATION:
            if self.pool is not None:
                self.pool.registry.take_pending(update.effective_user.id, "")
            await message.reply_text("Cancelled; no bot was created.", reply_markup=ReplyKeyboardRemove())
            return
        await message.reply_text(
            "I'm the control panel: I don't run an agent. Talk to one of your bots (/bots), or create one with "
            "/bot new <name> <task>. /help explains the rest."
        )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is not None and update.effective_message is not None and not self.config.is_allowed(user.id, user.username):
            await update.effective_message.reply_text("This is a private bot. Your Telegram user id is %s." % user.id)
            return
        await self._cmd_help(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._send(context.bot, chat_of(update), BOT_USAGE)

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_text(await usage.fetch(self.config, dict(os.environ)))

    async def _cmd_shared(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """The shared memory, written by you here (and by a bot only with the `shared` permission), read by every bot."""
        if not self._authorized(update):
            return
        await self._send(context.bot, chat_of(update), shared_memory.command(self.config, command_text(update.effective_message)))

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.pool is None:
            await update.effective_message.reply_text("Not running under `alfred run`.")
            return
        records = self.pool.registry.list(owner_id=update.effective_user.id)
        running = sum(1 for record in records if self.pool.is_running(record.bot_id))
        lines = [
            "Control panel @%s: %s" % (self.pool.manager_username or "?", self.pool.manager_state()),
            "Your bots: %d, %d connected" % (len(records), running),
            "Their directories: %s" % self.config.bots_home,
        ]
        await update.effective_message.reply_text("\n".join(lines))

    # -- the bots -------------------------------------------------------------

    async def _cmd_bots(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.pool is None:
            await update.effective_message.reply_text("Bots are managed by `alfred run`, which is not what is running.")
            return
        records = self.pool.registry.list(owner_id=update.effective_user.id)  # only yours
        if not records:
            await update.effective_message.reply_text("No bots yet. Create one with /bot new <name> [task].")
            return
        lines = []
        for record in records:
            task = record.settings.get("task") or "no task yet"
            state = "" if record.enabled else " (paused)"
            if record.enabled and not self.pool.is_running(record.bot_id):
                state = " (not connected)"
            flag = " ⚠️ CROSS-AGENT ACCESS" if record.cross_agent else ""
            lines.append("• %s %s%s%s\n%s" % (record.name, record.handle, state, flag, task.splitlines()[0][:120]))
        await self._send(context.bot, chat_of(update), "\n\n".join(lines))

    async def _cmd_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        reply = update.effective_message.reply_text
        if self.pool is None:
            await reply("Bots are managed by `alfred run`, which is not what is running.")
            return
        words = command_text(update.effective_message).split(None, 1)
        action = words[0].lower() if words else ""
        rest = words[1] if len(words) > 1 else ""
        if not action and self.menus is not None:
            session, screen = self.menus.open(update.effective_user.id, update.effective_chat.id)
            sent = await update.effective_message.reply_text(screen.text, reply_markup=self._markup(screen))
            session.message_id = getattr(sent, "message_id", None)
            return
        try:
            if action == "new":
                await self._bot_new(update, rest)
            elif action == "audit":
                await reply(self._audit_text(update.effective_user.id))
            elif action in ("base-soul", "basesoul"):
                await self._base_soul(update, rest)
            elif action in ("show", "set", "perm", "pause", "resume", "destroy", "soul"):
                target, _, rest = rest.strip().partition(" ")
                # Only the user's own bots: someone else's reads as "no such bot", so it is not even confirmed to exist.
                record = self.pool.registry.find(target, owner_id=update.effective_user.id) if target else None
                if record is None:
                    await reply("Which bot? Give its @username, name or id. /bots lists them.")
                    return
                if action == "show":
                    note = usage_monitor.permission_note(self.config, record)
                    await reply("%s\n\nPreset: %s%s" % (bots.describe(record, self.pool.is_running(record.bot_id)),
                                                        presets.level_name(record), "\n\n" + note if note else ""))
                elif action == "set":
                    await self._bot_set(update, record, rest)
                elif action == "perm":
                    await self._bot_perm(update, record, rest)
                elif action == "pause":
                    await self.pool.pause(record)
                    await reply("Paused %s. Messages sent to it meanwhile are dropped; its scheduled items wait until /bot resume." % record.handle)
                elif action == "resume":
                    await reply(await self.pool.resume(record))
                elif action == "soul":
                    await self._bot_soul(update, record, rest)
                else:
                    await self._bot_destroy_ask(update, record)
            else:
                await reply(BOT_USAGE)
        except (BotError, soul.SoulError) as exc:
            await reply(str(exc))

    async def _bot_soul(self, update: Update, record: BotRecord, rest: str) -> None:
        """/bot soul <bot> show | set <text> | reset: the owner reads and changes a bot's personality."""
        verb, _, text = rest.strip().partition(" ")
        verb = verb.lower() or "show"
        chat = chat_of(update)
        if verb == "show":
            current = soul.read(self.pool.registry.workspace(record.bot_id)) or soul.base(self.config)
            await self._send(self.bot, chat, "Personality of %s (SOUL.md):\n\n%s" % (record.handle, current.strip()))
        elif verb == "set":
            if not text.strip():
                await update.effective_message.reply_text("Give the new personality after set: /bot soul %s set <text>" % record.username)
                return
            await self.pool.set_soul(record, text)
            await update.effective_message.reply_text(
                "Personality of %s replaced. It applies from its next message, in the same conversation." % record.handle)
        elif verb == "reset":
            await self.pool.set_soul(record, None)
            await update.effective_message.reply_text(
                "Personality of %s reset to the base. It applies from its next message, in the same conversation." % record.handle)
        else:
            await update.effective_message.reply_text("/bot soul <bot> show | set <text> | reset")

    async def _base_soul(self, update: Update, rest: str) -> None:
        """/bot base-soul show | set <text> | reset: the personality new bots start with."""
        verb, _, text = rest.strip().partition(" ")
        verb = verb.lower() or "show"
        if verb == "show":
            edited = "" if soul.base_is_edited(self.config) else " (the template; not edited yet)"
            await self._send(self.bot, chat_of(update), "Base personality for new bots%s:\n\n%s" % (edited, soul.base(self.config).strip()))
        elif verb == "set" and text.strip():
            soul.set_base(self.config, text)
            await update.effective_message.reply_text("Base personality replaced. New bots start with it; existing ones keep theirs.")
        elif verb == "reset":
            soul.reset_base(self.config)
            await update.effective_message.reply_text("Base personality reset to the template. Existing bots keep theirs.")
        else:
            await update.effective_message.reply_text("/bot base-soul show | set <text> | reset")

    async def _bot_new(self, update: Update, rest: str) -> None:
        reply = update.effective_message.reply_text
        match = re.match(r"(\S+)\s*(.*)\Z", rest.strip(), re.S)
        if match is None:
            await reply("Give the new bot a name: /bot new <name> [options] [task]. /help lists the options.")
            return
        name = match.group(1)
        option_words, task = bots.split_options(match.group(2))
        settings, permissions, _ = bots.parse_options(self.config, option_words)
        if task:
            settings["task"] = bots.clean_setting(self.config, "task", task)
        await self.send_creation_request(update.effective_user.id, update.effective_chat.id, name, settings, permissions)

    async def send_creation_request(
        self, user_id: int, chat_id: int, name: str, settings: Dict[str, str], permissions: Dict[str, str],
        soul_text: str = "",
    ) -> str:
        """Ask Telegram to create a bot: a keyboard button, then the link. Returns the suggested username."""
        link, username = await self.pool.request_creation(user_id, chat_id, name, settings, permissions, soul_text)
        draft = bots.BotRecord(bot_id=0, username=username, name=name, owner_id=0, settings=settings, permissions=permissions)
        # First way: a keyboard button that asks Telegram to create the bot (KeyboardButtonRequestManagedBot).
        # python-telegram-bot 22.5 has no class for it, so the button carries the Bot API's own JSON.
        request = {"request_id": bots.new_request_id(), "suggested_name": name[:64], "suggested_username": username}
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("Create @%s" % username, api_kwargs={"request_managed_bot": request})],
             [KeyboardButton(CANCEL_CREATION)]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        await self.bot.send_message(
            chat_id=chat_id,
            text="To create it, tap \"Create @%s\" on the keyboard below and confirm in the window Telegram opens. "
            "You can change the name and username there. When it exists I connect to it and tell you here.\n\n"
            "If the keyboard doesn't show or the button does nothing (an older Telegram app), use the link in the "
            "next message instead.\n\nIt will start with %s (change it any time from /bot or with /bot perm):\n%s"
            % (username, presets.name_of(permissions), bots.permission_summary(draft, for_owner=True)),
            reply_markup=keyboard,
        )
        # Second way: the creation link. It works where the Telegram app opens t.me/newbot links itself.
        await self.bot.send_message(
            chat_id=chat_id,
            text="Second option, the link: %s" % link,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Create @%s by link" % username, url=link)]]),
        )
        return username

    async def _bot_set(self, update: Update, record: BotRecord, rest: str) -> None:
        setting, _, value = rest.strip().partition(" ")
        if not setting:
            await update.effective_message.reply_text("Which setting? One of: %s" % ", ".join(bots.SETTING_NAMES))
            return
        updated = await self.pool.set_setting(record, setting, value)
        stored = updated.settings.get(setting.lower())
        await update.effective_message.reply_text(
            "%s of %s: %s. It applies from its next message." % (setting.lower(), record.handle, stored or "default")
        )

    async def _bot_perm(self, update: Update, record: BotRecord, rest: str) -> None:
        name, _, value = rest.strip().partition(" ")
        if not name:
            await update.effective_message.reply_text(
                "Which permission? One of: %s, or cross (cross-agent access). Or a preset: %s."
                % (", ".join(bots.PERMISSION_NAMES), ", ".join(presets.keys())))
            return
        if name.lower() in bots.CROSS_AGENT_WORDS:
            await self._bot_cross(update, record, value)
            return
        # A preset as a shortcut: `/bot perm <bot> assistant` or `/bot perm <bot> preset assistant`.
        # It sets the permissions only; cross-agent access is kept as it is (it has its own command above).
        key = presets.find(value) if name.lower() == "preset" else (presets.find(name) if not value.strip() else None)
        if name.lower() == "preset" and key is None:
            await update.effective_message.reply_text("Which preset? One of: %s." % ", ".join(presets.keys()))
            return
        target = presets.apply(key, record.permissions) if key is not None else None
        if target is None and name.lower() == "admin" and value.strip().lower() in bots.ON_WORDS:
            target = dict(record.permissions, admin="on")
        if target is not None and target.get("admin") == "on" and not record.allows("admin"):
            await self._ask_admin(update, record, target)  # becoming a bot admin asks first, as cross-agent access
            return
        if key is not None:
            updated = await self.pool.set_permissions(record, presets.apply(key, record.permissions),
                                                      by=update.effective_user.id)
            backend = updated.settings.get("backend") or self.config.default_backend
            warnings = menus.permission_warnings(updated, backend)
            await update.effective_message.reply_text(
                "Permissions of %s: %s. It applies from its next message.\n\n%s%s" % (
                    record.handle, presets.level_name(updated), bots.permission_summary(updated, for_owner=True),
                    "".join("\n" + warning for warning in warnings))
            )
            return
        updated = await self.pool.set_permission(record, name, value, by=update.effective_user.id)
        note = usage_monitor.permission_note(self.config, updated)
        await update.effective_message.reply_text(
            "%s of %s: %s. It applies from its next message.\n\n%s%s" % (
                name.lower(), record.handle, updated.permission(name.lower()) or "none",
                bots.permission_summary(updated, for_owner=True), "\n\n" + note if note else "")
        )

    async def _bot_cross(self, update: Update, record: BotRecord, value: str) -> None:
        """Cross-agent access: on asks first, with a warning; off applies at once. Both are audited."""
        reply = update.effective_message.reply_text
        value = value.strip().lower()
        if value in bots.OFF_WORDS or value in bots.CLEAR_WORDS:
            if not record.cross_agent:
                await reply("%s has no cross-agent access. %s" % (record.handle, bots.cross_agent_line(record)))
                return
            updated = await self.pool.set_cross_agent(record, False, update.effective_user.id, "command")
            await reply("Cross-agent access withdrawn from %s. It applies from its next message.\n\n%s"
                        % (record.handle, bots.cross_agent_line(updated)))
            return
        if value not in bots.ON_WORDS:
            await reply("cross is on or off. %s" % bots.cross_agent_line(record))
            return
        if record.cross_agent:
            await reply(bots.cross_agent_line(record))
            return
        nonce = self.pool.ask_confirmation(record.bot_id, update.effective_user.id, "crossgrant")
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Grant cross-agent access", callback_data="bot:crossgrant:%s" % nonce),
            InlineKeyboardButton("Cancel", callback_data="bot:cancel:%s" % nonce),
        ]])
        await reply(bots.CROSS_AGENT_WARNING.format(name=record.name, handle=record.handle, username=record.username),
                    reply_markup=markup)

    async def _ask_admin(self, update: Update, record: BotRecord, target: Dict[str, str]) -> None:
        nonce = self.pool.ask_confirmation(record.bot_id, update.effective_user.id, "admingrant", payload=target)
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🧑‍💼 Make it a bot admin", callback_data="bot:admingrant:%s" % nonce),
            InlineKeyboardButton("Cancel", callback_data="bot:cancel:%s" % nonce),
        ]])
        await update.effective_message.reply_text(
            "%s\n\nIt would have: %s" % (bots.ADMIN_WARNING.format(handle=record.handle), presets.name_of(target)),
            reply_markup=markup)

    def _audit_text(self, user_id: int) -> str:
        """The audit lines about the user's own bots (docs/PANEL_ONLY.md: the open question of a global view)."""
        return bots.audit_report(self.config, user_id)

    async def _bot_destroy_ask(self, update: Update, record: BotRecord) -> None:
        nonce = self.pool.ask_confirmation(record.bot_id, update.effective_user.id, "destroy")
        scheduled = self.store.count_for_bot(record.bot_id)
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Destroy %s" % record.handle, callback_data="bot:destroy:%s" % nonce),
            InlineKeyboardButton("Cancel", callback_data="bot:cancel:%s" % nonce),
        ]])
        await update.effective_message.reply_text(
            "Destroy %s (%s)? This stops it, deletes its %d scheduled item(s), its conversations, memory and files, "
            "and revokes its token. It cannot be undone." % (record.name, record.handle, scheduled),
            reply_markup=markup,
        )

    @staticmethod
    def _markup(screen: "menus.Screen") -> Optional[InlineKeyboardMarkup]:
        if not screen.rows:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                     for row in screen.rows])

    async def _show(self, session: "menus.Session", screen: "menus.Screen") -> None:
        """Put a screen in the menu's own message; if that message can't be edited, send a new one."""
        if session.message_id is not None and self.bot is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=session.chat_id, message_id=session.message_id, text=screen.text, reply_markup=self._markup(screen)
                )
                return
            except Exception as exc:  # deleted, too old, or "message is not modified"
                if "not modified" in str(exc).lower():
                    return
                log.info("could not edit the menu message: %s", exc)
        if self.bot is not None:
            sent = await self.bot.send_message(chat_id=session.chat_id, text=screen.text, reply_markup=self._markup(screen))
            session.message_id = getattr(sent, "message_id", None)

    async def _on_menu_button(self, update: Update, token: str, action: str) -> None:
        query = update.callback_query
        session = self.menus.find(token, update.effective_user.id) if self.menus is not None else None
        if session is None:
            await query.answer("This menu has expired.")
            try:
                await query.edit_message_text(
                    "This menu has expired: it timed out, was replaced by a newer one, or Consiglieri restarted. "
                    "Nothing from it was kept. Send /bot to open a new one."
                )
            except Exception:
                pass
            return
        await query.answer()
        session.message_id = getattr(getattr(query, "message", None), "message_id", None) or session.message_id
        screen = await self.menus.press(session, action)
        if screen is None:
            screen = menus.Screen("Menu closed. Send /bot to open it again.")
        try:
            await query.edit_message_text(screen.text, reply_markup=self._markup(screen))
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                log.info("could not edit the menu message: %s", exc)
                await self._show(session, screen)

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(update) or self.pool is None:
            await query.answer()  # someone else's tap does nothing
            return
        parsed = menus.parse_callback(query.data or "")
        if parsed is not None:
            await self._on_menu_button(update, *parsed)
            return
        if not (query.data or "").startswith("bot:"):
            await query.answer()
            return
        _, action, nonce = (query.data.split(":", 2) + ["", ""])[:3]
        payload = self.pool.confirmation_payload(nonce)
        taken = self.pool.take_confirmation(nonce, update.effective_user.id, None if action == "cancel" else action)
        await query.answer()
        if taken is None:
            ask = {"crossgrant": "Ask again with /bot perm <bot> cross on.",
                   "admingrant": "Ask again with /bot perm <bot> admin on.",
                   }.get(action, "Ask again with /bot destroy.")
            await query.edit_message_text("That button has expired. %s" % ask)
            return
        bot_id, asked = taken
        if action == "cancel":
            await query.edit_message_text("Nothing was destroyed." if asked == "destroy" else "Nothing was granted.")
            return
        record = self.pool.registry.get(bot_id)
        if record is None:
            await query.edit_message_text("That bot is already gone.")
            return
        if int(record.owner_id) != int(update.effective_user.id):  # the nonce is the asker's; this is the second lock
            await query.edit_message_text("That bot is not yours.")
            return
        if action == "crossgrant":
            updated = await self.pool.set_cross_agent(record, True, update.effective_user.id, "command")
            await query.edit_message_text("Cross-agent access granted to %s. It applies from its next message.\n\n%s"
                                          % (record.handle, bots.cross_agent_line(updated)))
            return
        if action == "admingrant":
            updated = await self.pool.set_permissions(record, payload or dict(record.permissions, admin="on"),
                                                      by=update.effective_user.id, via="command")
            await query.edit_message_text("%s is now a bot admin (%s). It applies from its next message.\n\n%s"
                                          % (record.handle, presets.level_name(updated),
                                             bots.permission_summary(updated, for_owner=True)))
            return
        await query.edit_message_text("Destroying %s…" % record.handle)
        await self._send(context.bot, chat_of(update), await self.pool.destroy(record))

    async def _on_managed_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        payload = bots.managed_bot_payload(update)
        if payload is None or self.pool is None:
            return
        try:
            await self.pool.on_managed_bot(*payload)
        except Exception:
            log.exception("could not take over a managed bot")


class BotPool:
    """Runs the control panel and every bot Consiglieri created, in one process, and changes that set while it runs."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry = BotRegistry(config)
        self.store = SchedulerStore(config.db_path)
        self.main = ControlPanel(config, pool=self)
        self.frontends: Dict[int, TelegramBot] = {}  # the created bots that are connected, by bot id
        self.applications: Dict[int, Application] = {}
        self.manager_username = ""
        self.can_manage: Optional[bool] = None
        self._confirmations: Dict[str, Tuple[int, int, float, str]] = {}  # nonce -> bot, user, expiry, action
        self._payloads: Dict[str, Any] = {}  # nonce -> what a confirmation applies (the permissions it sets)
        self._scheduler: Optional["asyncio.Future[None]"] = None
        # One per process, not one per bot: the plan limits belong to the Claude account all the bots share.
        self.usage_monitor: Optional[usage_monitor.UsageMonitor] = None
        # Personality drafts: one Claude call each, made here in the panel's process, never by an agent.
        self.soul_writer = soul_writer.SoulWriter(config)
        self._lock_object: Optional[asyncio.Lock] = None

    @property
    def _lock(self) -> asyncio.Lock:
        # Made on first use, inside the running loop (on Python 3.9 a Lock is bound to the loop it is made in).
        if self._lock_object is None:
            self._lock_object = asyncio.Lock()
        return self._lock_object

    # -- connecting to Telegram (tests replace these two) -----------------------

    async def _connect(self, frontend: Any, token: str, updates: List[str], drop_pending: bool = False) -> Application:
        application = frontend.build(token)
        await application.initialize()  # also checks the token with getMe
        await frontend.started(application)
        await application.updater.start_polling(allowed_updates=updates, drop_pending_updates=drop_pending)
        await application.start()
        return application

    async def _disconnect(self, application: Application) -> None:
        if application.updater is not None and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self.applications[0] = await self._connect(self.main, self.config.telegram_token, MAIN_UPDATES)
        me = getattr(self.main.bot, "bot", None)  # the User that getMe returned
        self.manager_username = getattr(self.main.bot, "username", "") or ""
        self.can_manage = bots.can_manage_bots(me)
        for record in self.registry.list():
            if record.enabled:
                await self.launch(record)
        # Polled often because `alfred event` relies on it to wake the agent promptly.
        self._scheduler = asyncio.ensure_future(run_scheduler(self.store, self.deliver, poll_seconds=2.0))
        # Off unless ALFRED_USAGE_MONITOR is set. When on, it checks the bots' usage alerts with one reading per cycle.
        self.usage_monitor = usage_monitor.start(self.config, scheduler=self.store)
        log.info(
            "Consiglieri is up. home=%s backend=%s bots=%d running=%d manager=%s",
            self.config.home, self.config.default_backend, len(self.registry.list()),
            len(self.frontends), self.manager_state(),
        )

    async def stop(self) -> None:
        if self.usage_monitor is not None:
            await self.usage_monitor.stop()
        if self._scheduler is not None:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
        for bot_id in list(self.frontends):
            await self.shutdown_bot(bot_id)
        application = self.applications.pop(0, None)
        if application is not None:
            await self._disconnect(application)

    def manager_state(self) -> str:
        return {True: "bot management on", False: "bot management off in @BotFather", None: "bot management unknown"}[self.can_manage]

    def is_running(self, bot_id: int) -> bool:
        return bot_id in self.frontends

    # -- one bot -------------------------------------------------------------

    async def launch(self, record: BotRecord, drop_pending: bool = False) -> bool:
        """Connect a created bot and start answering in its chats. True if it is running afterwards.

        `drop_pending` discards what was sent to it while it was disconnected (a paused bot ignores messages).
        """
        async with self._lock:
            if record.bot_id in self.frontends:
                return True
            token = self.registry.token(record.bot_id)
            if not token:
                log.warning("%s has no token; not starting it", record.handle)
                return False
            config = self.registry.config_for(record.bot_id)
            bots.prepare_workspace(config)
            frontend = TelegramBot(config, record=record, pool=self)
            try:
                application = await self._connect(frontend, token, BOT_UPDATES, drop_pending)
            except Exception as exc:
                log.warning("could not connect %s: %s", record.handle, exc)
                await frontend.agent.shutdown()
                return False
            self.frontends[record.bot_id] = frontend
            self.applications[record.bot_id] = application
            log.info("connected %s", record.handle)
            return True

    async def shutdown_bot(self, bot_id: int) -> None:
        """Disconnect a created bot and close its agent's processes. Its conversations are kept on disk."""
        async with self._lock:
            frontend = self.frontends.pop(bot_id, None) if bot_id else None
            application = self.applications.pop(bot_id, None)
        if frontend is not None:
            await frontend.agent.shutdown()
            for chat in list(frontend._progress):
                await frontend._clear_progress(chat)
        if application is not None:
            try:
                await self._disconnect(application)
            except Exception as exc:
                log.warning("disconnecting bot %s: %s", bot_id, exc)

    def link_chat(self, bot_id: int, chat_id: int) -> None:
        record = self.registry.get(bot_id)
        if record is not None and int(chat_id) not in record.chat_ids:
            record.chat_ids.append(int(chat_id))
            self.registry.save(record)
            frontend = self.frontends.get(bot_id)
            if frontend is not None:
                frontend.record = record

    async def deliver(self, item: Item) -> None:
        """Hand a due item to the bot that scheduled it, never to another one."""
        bot_id = int(item.get("bot_id") or 0)
        if not bot_id:
            await self.main._deliver(item)
            return
        frontend = self.frontends.get(bot_id)
        if frontend is not None:
            await frontend._deliver(item)
            return
        record = self.registry.get(bot_id)
        if record is not None and (not record.enabled or self.registry.token(bot_id)):
            raise Postpone(300, "%s is paused or not connected" % record.handle)
        raise RuntimeError("bot %s no longer exists" % bot_id)

    # -- creating ------------------------------------------------------------

    async def request_creation(
        self, owner_id: int, chat_id: int, name: str, settings: Dict[str, str], permissions: Optional[Dict[str, str]] = None,
        soul_text: str = "",
    ) -> Tuple[str, str]:
        """Remember what the user asked for and return the link that creates the bot, and its username."""
        if self.can_manage is False:
            raise BotError(
                "I can't create bots yet: Bot Management Mode is off for me. Open @BotFather, tap Open (its Mini App), "
                "choose me, and turn on Bot Management Mode in my settings. Then try again."
            )
        if not self.manager_username:
            raise BotError("I don't know my own username yet; try again in a moment.")
        username = self.suggest_username(name, settings.get("task", ""))
        self.registry.add_pending(owner_id, chat_id, name, username, settings, permissions,
                                  soul.clean(soul_text) if soul_text.strip() else "")
        return bots.creation_link(self.manager_username, username, name), username

    def suggest_username(self, name: str, task: str = "") -> str:
        """The panel's username, extended: manolito_bot -> manolito_<what the bot does>_bot."""
        return bots.suggest_username(name, self.manager_username, task, self.registry.known_usernames())

    async def on_managed_bot(self, creator: Dict[str, Any], bot: Dict[str, Any]) -> Optional[BotRecord]:
        """Telegram says a bot managed by Consiglieri was created, or its token or owner changed."""
        creator_id = int(creator["id"])
        bot_id = int(bot["id"])
        username = str(bot.get("username") or "")
        existing = self.registry.get(bot_id)
        if not self.config.is_allowed(creator_id, creator.get("username")):
            if existing is not None:
                # One of our bots now belongs to someone else (ownership was transferred): let go of it.
                log.warning("%s now belongs to unauthorized user id=%s; disconnecting it", existing.handle, creator_id)
                await self.pause(existing)
                self.registry.delete_token(bot_id)
                await self.set_cross_agent(existing, False, None, "ownership moved to user %s" % creator_id)
                await self._notify(existing.notify_chat_id or existing.owner_id,
                                   "%s now belongs to someone else, so I disconnected it and forgot its token. "
                                   "/bot destroy %s removes what is left of it here." % (existing.handle, existing.username))
                return None
            # Anyone who knows the panel's username can open a creation link. Their bot is never taken over.
            log.warning("ignored managed bot %s created by unauthorized user id=%s", bot_id, creator_id)
            return None
        manager = self.main.bot
        if existing is not None:
            return await self._token_changed(existing, creator_id)

        pending = self.registry.take_pending(creator_id, username)
        record = BotRecord(
            bot_id=bot_id,
            username=username,
            name=str(bot.get("first_name") or (pending.name if pending else username)),
            owner_id=creator_id,
            settings=dict(pending.settings) if pending else {},
            # Without a request of its own, a bot gets the defaults: the least it can do.
            permissions=dict(pending.permissions) if pending else {},
            notify_chat_id=pending.chat_id if pending else creator_id,
        )
        token = await bots.fetch_token(manager, bot_id)
        self.registry.save(record)
        self.registry.set_token(bot_id, token)
        if pending is not None and pending.soul.strip():  # its own personality from the assistant; else the base
            soul.write(self.registry.workspace(bot_id), pending.soul)
        try:
            await bots.restrict_access(manager, bot_id)
        except Exception as exc:  # Consiglieri's own allow-list still guards the bot
            log.warning("could not restrict access to %s: %s", record.handle, exc)
        running = await self.launch(record)
        if running:
            await self._sync_profile(record)
        lines = ["%s is ready: %s" % (record.handle, record.link)]
        lines.append("Open it and press Start to begin. Its chat, memory, tools and Claude session are its own.")
        lines.append(bots.permission_summary(record, for_owner=True))
        if not pending:
            lines.append("(I hadn't asked for it, but you created it with me as manager, so I took it on.)")
        if not record.settings.get("task"):
            lines.append("Give it a job with /bot set %s task <what it should do>." % record.username)
        if not running:
            lines = ["%s was created, but I could not connect to it. Try /bot resume %s." % (record.handle, record.username)]
        # This message also takes the creation keyboard away.
        await self._notify(record.notify_chat_id, "\n".join(lines), reply_markup=ReplyKeyboardRemove())
        return record

    async def _token_changed(self, record: BotRecord, creator_id: int) -> BotRecord:
        token = await bots.fetch_token(self.main.bot, record.bot_id)
        if token != self.registry.token(record.bot_id):
            self.registry.set_token(record.bot_id, token)
            if record.enabled:
                await self.shutdown_bot(record.bot_id)
                await self.launch(record)
            await self._notify(record.notify_chat_id or record.owner_id, "%s has a new token; I reconnected to it." % record.handle)
        if creator_id != record.owner_id:
            record.owner_id = creator_id
            self.registry.save(record)
        return record

    async def _sync_profile(self, record: BotRecord) -> None:
        """Show the bot's task as its short description, the text on its profile and before Start."""
        frontend = self.frontends.get(record.bot_id)
        task = record.settings.get("task") or ""
        if frontend is None or frontend.bot is None:
            return
        try:
            await frontend.bot.set_my_short_description(short_description=task[:120])
            await frontend.bot.set_my_description(description=task[:512])
        except Exception as exc:
            log.warning("could not update the description of %s: %s", record.handle, exc)

    async def _notify(self, chat_id: int, text: str, **extra: Any) -> None:
        if self.main.bot is not None and chat_id:
            try:
                await TelegramBot._send(self.main.bot, chat_id, text, **extra)
            except Exception as exc:
                log.warning("could not tell chat %s: %s", chat_id, exc)

    # -- configuring ----------------------------------------------------------

    async def set_setting(self, record: BotRecord, setting: str, value: str) -> BotRecord:
        cleaned = bots.clean_setting(self.config, setting, value)
        record = self.registry.get(record.bot_id) or record
        name = setting.strip().lower()
        if cleaned:
            record.settings[name] = cleaned
        else:
            record.settings.pop(name, None)
        self.registry.save(record)
        frontend = self.frontends.get(record.bot_id)
        if frontend is not None:
            frontend.record = record
            # Model, effort, working directory and task reach the agent by themselves at its next message
            # (the process is reopened when its command or manual changes). A new backend needs the old one closed.
            if name == "backend":
                await frontend.agent.shutdown()
            if name == "task":
                await self._sync_profile(record)
        return record

    async def set_permission(self, record: BotRecord, permission: str, value: str, by: Optional[int] = None,
                             via: str = "command") -> BotRecord:
        cleaned = bots.clean_permission(self.config, permission, value)
        record = self.registry.get(record.bot_id) or record
        was_admin = record.allows("admin")
        record.permissions[permission.strip().lower()] = cleaned
        self.registry.save(record)
        record = self.registry.get(record.bot_id) or record
        self._audit_admin(record, was_admin, by, via)
        frontend = self.frontends.get(record.bot_id)
        if frontend is not None:
            frontend.record = record
            # The new rules are written before its next message and the process is reopened with them (its
            # command line and manual change). A turn in progress finishes under the old rules.
        return record

    async def set_permissions(self, record: BotRecord, permissions: Dict[str, str], by: Optional[int] = None,
                              via: str = "command") -> BotRecord:
        """Replace all of a bot's permissions at once (a preset): checked, then saved in one write."""
        cleaned = {name: bots.clean_permission(self.config, name, value) for name, value in permissions.items()}
        record = self.registry.get(record.bot_id) or record
        was_admin = record.allows("admin")
        record.permissions = cleaned
        self.registry.save(record)
        record = self.registry.get(record.bot_id) or record
        self._audit_admin(record, was_admin, by, via)
        frontend = self.frontends.get(record.bot_id)
        if frontend is not None:
            frontend.record = record  # applies from its next message, as with set_permission
        return record

    def _audit_admin(self, record: BotRecord, was_admin: bool, by: Optional[int], via: str) -> None:
        """Becoming a bot admin, or ceasing to be one, goes to the audit log like cross-agent access."""
        if record.allows("admin") != was_admin:
            event = "admin_permission_granted" if record.allows("admin") else "admin_permission_revoked"
            bots.audit(self.config, event, record, by, via)
            log.warning("%s %s", event.replace("_", " "), record.handle)

    async def set_soul(self, record: BotRecord, text: Optional[str]) -> str:
        """Replace a bot's personality, or with None put the base back. Its next message reopens its process
        with the same conversation, as with any change to its manual. Returns what it now is."""
        workspace = self.registry.workspace(record.bot_id)
        written = soul.reset(self.config, workspace) if text is None else soul.write(workspace, text)
        log.info("personality of %s %s (%d characters)", record.handle, "reset" if text is None else "replaced", len(written))
        return written

    async def pause(self, record: BotRecord) -> None:
        record = self.registry.get(record.bot_id) or record
        record.enabled = False
        self.registry.save(record)
        await self.shutdown_bot(record.bot_id)

    async def resume(self, record: BotRecord) -> str:
        record = self.registry.get(record.bot_id) or record
        record.enabled = True
        self.registry.save(record)
        if not self.registry.token(record.bot_id) and self.main.bot is not None:
            self.registry.set_token(record.bot_id, await bots.fetch_token(self.main.bot, record.bot_id))
        if await self.launch(record, drop_pending=True):
            return "%s is connected again: %s" % (record.handle, record.link)
        return "I could not connect to %s. If you deleted it in @BotFather, destroy it here with /bot destroy %s." % (
            record.handle, record.username)

    # -- destroying -----------------------------------------------------------

    def ask_confirmation(self, bot_id: int, user_id: int, action: str = "destroy", payload: Any = None,
                         seconds: float = CONFIRM_SECONDS) -> str:
        now = time.time()
        self._confirmations = {key: value for key, value in self._confirmations.items() if value[2] > now}
        self._payloads = {key: value for key, value in self._payloads.items() if key in self._confirmations}
        nonce = secrets.token_urlsafe(9)
        self._confirmations[nonce] = (int(bot_id), int(user_id), now + seconds, action)
        if payload is not None:
            self._payloads[nonce] = payload
        return nonce

    def confirmation_payload(self, nonce: str) -> Any:
        """What a confirmation applies; read it before take_confirmation, which forgets the button."""
        return self._payloads.get(nonce)

    def take_confirmation(self, nonce: str, user_id: int, action: Optional[str] = None) -> Optional[Tuple[int, str]]:
        """(bot, action) of a confirmation button, if it is fresh, pressed by the person who asked, and (when
        `action` is given) for that action: a destroy button never grants, a grant button never destroys."""
        entry = self._confirmations.get(nonce)
        if entry is None or entry[2] < time.time() or entry[1] != int(user_id):
            return None
        if action is not None and entry[3] != action:
            return None
        del self._confirmations[nonce]
        return entry[0], entry[3]

    # -- cross-agent access ---------------------------------------------------

    async def set_cross_agent(self, record: BotRecord, on: bool, by: Optional[int], via: str) -> BotRecord:
        """Grant or withdraw cross-agent access. The control panel is the only caller: a bot reaches neither
        the panel's buttons nor this process. Every change is written to the audit log."""
        record = self.registry.get(record.bot_id) or record
        if record.cross_agent == bool(on):
            return record
        record.cross_agent = bool(on)
        record.cross_agent_since = bots.utc_now_text() if on else ""
        self.registry.save(record)
        bots.audit(self.config, "cross_agent_granted" if on else "cross_agent_revoked", record, by, via)
        log.warning("cross-agent access %s %s (by %s, %s)", "granted to" if on else "withdrawn from", record.handle, by, via)
        record = self.registry.get(record.bot_id) or record
        frontend = self.frontends.get(record.bot_id)
        if frontend is not None:
            frontend.record = record  # new rules and manual from its next message, in a new process
        return record

    async def destroy(self, record: BotRecord) -> str:
        """Stop a bot, delete its schedule and state, and revoke its token. Returns what to tell the user."""
        await self.shutdown_bot(record.bot_id)
        current = self.registry.get(record.bot_id)
        if current is not None and current.cross_agent:
            bots.audit(self.config, "cross_agent_revoked", current, None, "destroy")
        deleted = self.store.delete_for_bot(record.bot_id)
        revoked = False
        if self.main.bot is not None:
            try:
                await bots.revoke_token(self.main.bot, record.bot_id)
                revoked = True
            except Exception as exc:
                log.warning("could not revoke the token of %s: %s", record.handle, exc)
        self.registry.delete_token(record.bot_id)
        self.registry.remove(record.bot_id)
        lines = [
            "Destroyed %s: stopped, %d scheduled item(s) deleted, conversations, memory and files removed, "
            "token %s." % (record.handle, deleted, "revoked" if revoked else "deleted here (Telegram did not confirm revoking it)"),
            "Telegram has no way for me to delete the bot itself. To delete it and free its username, open @BotFather "
            "and send /deletebot: https://t.me/BotFather?start=deletebot",
        ]
        return "\n".join(lines)


async def serve(config: Config) -> None:
    pool = BotPool(config)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - not on this platform
            pass
    await pool.start()
    try:
        await stopping.wait()
    finally:
        log.info("stopping")
        await pool.stop()


def run(config: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and a Bot API URL contains the bot's token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    home.prepare(config)
    log.info("browser: %s", orca.summary(config))
    asyncio.run(serve(config))
