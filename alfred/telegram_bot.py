"""The Telegram front end: authorization, commands, attachments, progress, and scheduled deliveries.

Receiving and replying are separate. A message is handed to the agent and the
handler returns; the agent's results arrive later through the `Listener`
methods, whenever a turn ends, including turns the agent started by itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from . import home, orca, usage, usage_monitor
from .agent import Agent, Listener
from .backends import BACKENDS, BackendError, RunResult
from .config import Config
from .scheduler import KIND_EVENT, KIND_TASK, Item, SchedulerStore, parse_time, run_scheduler
from .text import split_message

log = logging.getLogger("alfred.telegram")

# The single list of Telegram commands, in the order /help and the command menu show them.
# To add one: add a line here and a `_cmd_<name>` method on TelegramBot.
COMMANDS = [
    ("help", "Show what Alfred can do and the commands"),
    ("new", "Start a fresh conversation (memory and skills are kept)"),
    ("stop", "Stop what Alfred is doing right now"),
    ("usage", "How much of the Claude plan limits is used"),
    ("status", "Backend, session, browser and home directory"),
    ("memory", "List what Alfred has memorized"),
    ("skills", "List the skills Alfred has"),
    ("schedule", "List pending reminders and tasks"),
    ("backend", "Show or switch the agent backend: claude or codex"),
]
PROGRESS_INTERVAL = 2.5
TYPING_INTERVAL = 4.0
# The agent's whole final message when it has nothing to add to what it already sent.
NO_REPLY = "NO_REPLY"
UNSAFE_FILENAME = re.compile(r"[^\w.\-]+")
ATTACHMENT_KINDS = (("document", ""), ("voice", ".ogg"), ("audio", ".mp3"), ("video", ".mp4"), ("video_note", ".mp4"))


class Progress:
    """A typing indicator plus one status message that is edited as the agent uses tools."""

    def __init__(self, bot: Any, chat_id: int, show_tools: bool) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.show_tools = show_tools
        self._message: Optional[Message] = None
        self._last_edit = 0.0
        self._typing: Optional["asyncio.Future[None]"] = None

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.TYPING)
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
                    chat_id=self.chat_id, text=text, disable_notification=True
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


class TelegramBot(Listener):
    def __init__(self, config: Config, agent: Optional[Agent] = None) -> None:
        self.config = config
        self.agent = agent or Agent(config)
        self.agent.listener = self
        self.store = SchedulerStore(config.db_path)
        self.bot: Any = None  # set when the application starts, or by the first update
        self._progress: Dict[int, Progress] = {}
        self._scheduler: Optional["asyncio.Future[None]"] = None
        self.usage_monitor: Optional[usage_monitor.UsageMonitor] = None

    # -- wiring -------------------------------------------------------------

    def build(self) -> Application:
        application = (
            ApplicationBuilder()
            .token(self.config.telegram_token)
            .concurrent_updates(True)  # so /stop is handled while a turn is running
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        for name in ["start"] + [name for name, _ in COMMANDS]:
            application.add_handler(CommandHandler(name, getattr(self, "_cmd_%s" % name)))
        # Everything else, including unknown /commands (which invoke a skill by name), goes to the agent.
        application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL & ~filters.UpdateType.EDITED, self._on_message))
        application.add_error_handler(self._on_error)
        return application

    async def _post_init(self, application: Application) -> None:
        await application.bot.set_my_commands([BotCommand(name, description) for name, description in COMMANDS])
        self.bot = application.bot
        # Polled often because `alfred event` relies on it to wake the agent promptly.
        self._scheduler = asyncio.ensure_future(run_scheduler(self.store, self._deliver, poll_seconds=2.0))
        # Off unless ALFRED_USAGE_MONITOR is set; even then it only logs a line for now.
        self.usage_monitor = usage_monitor.start(self.config)
        log.info("Alfred is up. home=%s backend=%s", self.config.home, self.config.default_backend)

    async def _post_shutdown(self, application: Application) -> None:
        if self._scheduler is not None:
            self._scheduler.cancel()
        await self.agent.shutdown()

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("update failed", exc_info=context.error)

    # -- helpers ------------------------------------------------------------

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None or update.effective_chat is None or update.effective_message is None:
            return False
        if self.config.is_allowed(user.id, user.username):
            # No message content in the log; just enough to see that an update arrived.
            text = update.effective_message.text or ""
            kind = text.split()[0] if text.startswith("/") else "message"
            log.info("%s from user id=%s in chat %s", kind, user.id, update.effective_chat.id)
            return True
        log.warning("ignored message from unauthorized user id=%s username=%s", user.id, user.username)
        return False

    @staticmethod
    async def _send(bot: Any, chat_id: int, text: str) -> None:
        for chunk in split_message(text) or ["(empty reply)"]:
            await bot.send_message(chat_id=chat_id, text=chunk)

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
        log.info("turn ended in chat %s: %s", chat_id, "error" if result.is_error else "posted" if parts else "nothing to post")
        if parts and self.bot is not None:
            await self._send(self.bot, chat_id, "\n\n".join(parts))

    async def _deliver(self, item: Item) -> None:
        """A scheduled item came due: post a fixed message, or wake the agent with a task or an event."""
        if item["kind"] in (KIND_TASK, KIND_EVENT):
            label, source = ("Scheduled task", "schedule") if item["kind"] == KIND_TASK else ("Event", "event")
            await self.agent.submit(
                item["chat_id"],
                "[%s] %s" % (label, item["message"]),
                user_id=item["user_id"] or None,
                username=item["username"] or None,
                source=source,
            )
        elif self.bot is not None:
            await self._send(self.bot, item["chat_id"], "⏰ %s" % item["message"])

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
            update.effective_chat.id,
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
        await self._cmd_help(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        lines = [
            "I'm Alfred. Just write to me in plain language. You can also send photos, files and voice notes.",
            "",
            "Commands:",
        ]
        lines += ["/%s - %s" % command for command in COMMANDS]
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
        await self.agent.new_conversation(update.effective_chat.id)
        await self._clear_progress(update.effective_chat.id)
        await update.effective_message.reply_text("Fresh conversation. I still have my memory and skills.")

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        was_working = await self.agent.stop(update.effective_chat.id)
        await self._clear_progress(update.effective_chat.id)
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
        await self._send(context.bot, update.effective_chat.id, "\n".join(lines))

    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        skills = home.list_skills(self.config)
        if not skills:
            await update.effective_message.reply_text("No skills yet.")
            return
        lines = ["%d skill(s):" % len(skills)]
        lines += ["\n• %s\n%s" % (skill.name, skill.description) for skill in skills]
        await self._send(context.bot, update.effective_chat.id, "\n".join(lines))

    async def _cmd_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        items = self.store.list(update.effective_chat.id)
        if not items:
            await update.effective_message.reply_text("Nothing scheduled.")
            return
        lines = []
        for item in items:
            when = parse_time(item["due_at_utc"]).astimezone().strftime("%a %d %b %H:%M")
            repeat = " (repeats)" if item["repeat_seconds"] else ""
            lines.append("%s  %s%s  [%s]\n%s" % (item["id"][:8], when, repeat, item["kind"], item["message"]))
        await self._send(context.bot, update.effective_chat.id, "\n\n".join(lines))

    async def _cmd_backend(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        chat_id = update.effective_chat.id
        if not context.args:
            await update.effective_message.reply_text(
                "Backend: %s. Switch with /backend %s." % (self.agent.backend_name(chat_id), " | ".join(sorted(BACKENDS)))
            )
            return
        try:
            await self.agent.switch_backend(chat_id, context.args[0].lower())
        except BackendError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await self._clear_progress(chat_id)
        await update.effective_message.reply_text(
            "Now using %s for this chat. Each backend keeps its own conversation; memory and skills are shared."
            % self.agent.backend_name(chat_id)
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        chat_id = update.effective_chat.id
        backend = self.agent.backend_name(chat_id)
        pid = self.agent.live_pid(chat_id)
        lines = [
            "Backend: %s" % backend,
            "Model: %s" % self.agent.model(chat_id),
            "Effort: %s" % self.agent.effort(chat_id),
            "Session: %s" % (self.agent.sessions.session(chat_id, backend) or "none yet"),
            "Busy: %s" % ("yes" if self.agent.is_busy(chat_id) else "no"),
            "Process: %s" % (("open, pid %s" % pid) if pid else "not running; it starts with your next message"),
            "Memories: %d" % len(home.list_memories(self.config)),
            "Skills: %d" % len(home.list_skills(self.config)),
            "Scheduled: %d" % len(self.store.list(chat_id)),
            "Browser: %s" % await asyncio.get_event_loop().run_in_executor(None, orca.summary, self.config),
            "Home: %s" % self.config.home,
        ]
        await update.effective_message.reply_text("\n".join(lines))


def run(config: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    home.bootstrap(config)
    log.info("browser: %s", orca.summary(config))
    TelegramBot(config).build().run_polling()
