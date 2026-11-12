"""The agent core: one conversation per chat, routed to a backend running in the home directory.

Two ways in. `Agent.submit` hands a message over and returns at once; replies
come back through a `Listener`. With Claude this feeds a process that stays
open for the chat, so a message can land in the middle of a turn and the agent
can wake up by itself. `Agent.ask` runs exactly one turn and returns its
result; the terminal chat uses it, and so do backends that cannot stay open.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Set, Tuple

from . import extensions, home, orca
from .backends import (
    BACKENDS, Backend, BackendError, LiveEvents, LiveSession, ProgressCallback, RunResult, create_backend,
)
from .config import Config

log = logging.getLogger("alfred.agent")

Snapshot = Tuple[Set[str], Dict[str, str], Dict[str, str]]  # local skill names, memory, extensions


class Chat(int):
    """A conversation inside one bot: the Telegram chat id plus, in a chat with topics, the topic.

    It is an int so that code and callers that only know chat ids keep working: `Chat(7) == 7`, and both
    hash the same. A topic makes it a different conversation: `Chat(7, 3) != Chat(7)`. Which bot it
    belongs to is not part of it, because every bot has its own `Agent` and its own session file.
    """

    thread_id: Optional[int]

    def __new__(cls, chat_id: int, thread_id: Optional[int] = None) -> "Chat":
        if isinstance(chat_id, Chat) and thread_id is None:
            return chat_id
        chat = int.__new__(cls, int(chat_id))
        chat.thread_id = int(thread_id) if thread_id else None
        return chat

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Chat):
            return int(self) == int(other) and self.thread_id == other.thread_id
        if isinstance(other, int):
            return self.thread_id is None and int(self) == other
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        equal = self.__eq__(other)
        return equal if equal is NotImplemented else not equal

    def __hash__(self) -> int:
        return int.__hash__(self) if self.thread_id is None else hash((int(self), self.thread_id))

    def __repr__(self) -> str:
        return "Chat(%d%s)" % (int(self), ", %d" % self.thread_id if self.thread_id else "")

    @property
    def key(self) -> str:
        """How the conversation is stored: the bare chat id, as before topics existed, or `<chat>/<topic>`."""
        return str(int(self)) if self.thread_id is None else "%d/%d" % (int(self), self.thread_id)

    def send_kwargs(self) -> Dict[str, int]:
        """Extra arguments for Telegram's send methods, so a reply lands in the same topic."""
        return {} if self.thread_id is None else {"message_thread_id": self.thread_id}


class SessionStore:
    """Per-chat state on disk: the chosen backend and each backend's session id."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def _chat(self, data: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        chat = data.setdefault(Chat(chat_id).key, {})
        chat.setdefault("sessions", {})
        return chat

    def backend(self, chat_id: int) -> Optional[str]:
        return self._load().get(Chat(chat_id).key, {}).get("backend")

    def set_backend(self, chat_id: int, name: str) -> None:
        data = self._load()
        self._chat(data, chat_id)["backend"] = name
        self._save(data)

    def session(self, chat_id: int, backend: str) -> Optional[str]:
        return self._load().get(Chat(chat_id).key, {}).get("sessions", {}).get(backend)

    def set_session(self, chat_id: int, backend: str, session_id: Optional[str]) -> None:
        data = self._load()
        sessions = self._chat(data, chat_id)["sessions"]
        if session_id:
            sessions[backend] = session_id
        else:
            sessions.pop(backend, None)
        self._save(data)

    def reset(self, chat_id: int) -> None:
        data = self._load()
        self._chat(data, chat_id)["sessions"] = {}
        self._save(data)


def context_line(
    source: str, chat_id: int, user_id: Optional[int], username: Optional[str], bot: Optional[str] = None
) -> str:
    now = datetime.now().astimezone()
    chat = Chat(chat_id)
    parts = ['source="%s"' % source]
    if bot:
        parts.append('bot="@%s"' % bot.lstrip("@"))
    parts.append('chat_id="%d"' % chat)
    if chat.thread_id:
        parts.append('topic_id="%d"' % chat.thread_id)
    parts += [
        'time="%s"' % now.strftime("%Y-%m-%dT%H:%M%z"),
        'weekday="%s"' % now.strftime("%A"),
    ]
    if username:
        parts.append('user="@%s"' % username.lstrip("@"))
    if user_id is not None:
        parts.append('user_id="%s"' % user_id)
    return "<context %s />" % " ".join(parts)


class Listener:
    """Where `Agent.submit` reports. Subclass and override; every call names the chat."""

    async def turn_started(self, chat_id: int) -> None:
        """The agent began working for this chat. May be called again during the same turn."""

    async def progress(self, chat_id: int, note: str) -> None:
        """The agent started using a tool."""

    async def result(self, chat_id: int, result: RunResult) -> None:
        """A turn ended. One result can answer several messages, and some answer none (a self-started turn)."""


class _ChatEvents(LiveEvents):
    """What the agent does around a live session's turns: session id, skill and memory notes, autosave."""

    def __init__(self, agent: "Agent", chat_id: int, backend: str) -> None:
        self.agent = agent
        self.chat_id = chat_id
        self.backend = backend
        self.active = True  # cleared when the chat's session is dropped, so late events are ignored
        self.summary = ""
        self._before: Optional[Snapshot] = None

    async def turn_started(self) -> None:
        if not self.active:
            return
        if self._before is None:
            self._before = self.agent._snapshot()
        await self.agent.listener.turn_started(self.chat_id)

    async def progress(self, note: str) -> None:
        if self.active:
            await self.agent.listener.progress(self.chat_id, note)

    async def session_started(self, session_id: str) -> None:
        if self.active:
            self.agent.sessions.set_session(self.chat_id, self.backend, session_id)

    async def result(self, result: RunResult) -> None:
        if not self.active:
            return
        before, self._before = self._before, None
        if before is not None:
            self.agent._note_changes(result, before)
        await self.agent._autosave(self.chat_id, self.summary or "woke up by itself")
        self.summary = ""
        await self.agent.listener.result(self.chat_id, result)


class _Live(NamedTuple):
    session: LiveSession
    events: _ChatEvents
    settings: Any  # what the process was started with; when the configuration differs, it is reopened


class Agent:
    def __init__(self, config: Config, listener: Optional[Listener] = None) -> None:
        self.config = config
        self.listener = listener or Listener()
        self.sessions = SessionStore(config.sessions_path)
        self._backends: Dict[str, Backend] = {}
        self._locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._live: Dict[int, _Live] = {}
        self._turns: Dict[int, Set["asyncio.Future[None]"]] = defaultdict(set)
        # The bot's @username, for the context line of a bot Consiglieri created. None for the main bot.
        self.bot_username: Optional[str] = None

    # -- backend selection --------------------------------------------------

    def backend_name(self, chat_id: int) -> str:
        name = self.sessions.backend(chat_id) or self.config.default_backend
        return name if name in BACKENDS else "claude"

    def set_backend(self, chat_id: int, name: str) -> None:
        if name not in BACKENDS:
            raise BackendError("unknown backend %r (available: %s)" % (name, ", ".join(sorted(BACKENDS))))
        self.sessions.set_backend(chat_id, name)

    def _backend(self, name: str) -> Backend:
        if name not in self._backends:
            self._backends[name] = create_backend(name, self.config)
        return self._backends[name]

    def reset(self, chat_id: int) -> None:
        """Forget the conversation. Memory and skills live on disk and are unaffected."""
        self.sessions.reset(chat_id)

    async def new_conversation(self, chat_id: int) -> None:
        """Stop what is running for the chat and forget its conversation."""
        await self.stop(chat_id)
        self.reset(chat_id)

    async def switch_backend(self, chat_id: int, name: str) -> None:
        if name not in BACKENDS:
            raise BackendError("unknown backend %r (available: %s)" % (name, ", ".join(sorted(BACKENDS))))
        if name != self.backend_name(chat_id):
            await self.stop(chat_id)
        self.set_backend(chat_id, name)

    def is_busy(self, chat_id: int) -> bool:
        live = self._live.get(chat_id)
        return self._locks[chat_id].locked() or bool(self._turns.get(chat_id)) or bool(live and live.session.busy)

    def model(self, chat_id: int) -> str:
        """The model the chat's next message will use, as far as it is known."""
        name = self.backend_name(chat_id)
        configured = self.config.codex_model if name == "codex" else self.config.claude_model
        live = self._live.get(chat_id)
        running = live.session.model if live and live.settings == self._backend(name).live_settings() else None  # type: ignore[attr-defined]
        if running and configured and running != configured:
            return "%s (%s)" % (configured, running)
        return running or configured or "the %s default" % name

    def effort(self, chat_id: int) -> str:
        """The effort level set for the chat's backend, or a note that the backend decides."""
        name = self.backend_name(chat_id)
        configured = self.config.codex_effort if name == "codex" else self.config.claude_effort
        return configured or "the %s default" % name

    def live_pid(self, chat_id: int) -> Optional[int]:
        """The process kept open for this chat, if there is one right now."""
        live = self._live.get(chat_id)
        return live.session.pid if live else None

    # -- bookkeeping shared by both ways of running a turn -------------------

    def _snapshot(self) -> Snapshot:
        local = {skill.name for skill in home.list_skills(self.config) if skill.source == "local"}
        return local, home.memory_snapshot(self.config), extensions.inventory(self.config)

    def _note_changes(self, result: RunResult, before: Snapshot) -> None:
        skills, memory, added = self._snapshot()
        result.new_skills = sorted(skills - before[0])
        result.memory_changes = home.memory_changes(before[1], memory)
        result.extension_changes = extensions.changes(before[2], added, self.config)

    async def _autosave(self, chat_id: int, text: str) -> None:
        summary = " ".join(text.split())[:60]
        await asyncio.get_event_loop().run_in_executor(
            None, home.autosave, self.config, "chat %s: %s" % (Chat(chat_id).key, summary)
        )

    # -- messages in, results out through the listener -----------------------

    async def submit(
        self,
        chat_id: int,
        text: str,
        *,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        source: str = "telegram",
    ) -> None:
        """Hand a message to the chat's agent and return. The listener gets what comes of it."""
        chat_id = Chat(chat_id)
        name = self.backend_name(chat_id)
        backend = self._backend(name)
        if not (backend.supports_live and self.config.live):
            turn = asyncio.ensure_future(
                self._single_turn(chat_id, text, user_id=user_id, username=username, source=source)
            )
            self._turns[chat_id].add(turn)
            turn.add_done_callback(self._turns[chat_id].discard)
            return

        self._prepare_workspace()
        live = self._live.get(chat_id)
        settings = backend.live_settings()  # type: ignore[attr-defined]
        if live is not None and live.settings != settings and not live.session.busy:
            # The model or another setting was changed in .env: reopen with it. The conversation resumes.
            await self.stop(chat_id)
            live = None
        if live is None:
            events = _ChatEvents(self, chat_id, name)
            session = backend.open_live(  # type: ignore[attr-defined]
                session_id=self.sessions.session(chat_id, name),
                cwd=self.config.workspace,
                env=self._environment(chat_id, user_id, username),
                events=events,
            )
            live = self._live[chat_id] = _Live(session, events, settings)
        live.events.summary = text
        try:
            await live.session.send("\n\n".join([self._context(source, chat_id, user_id, username), text]))
        except BackendError as exc:
            self._live.pop(chat_id, None)
            await self.listener.result(chat_id, RunResult(text="%s backend failed: %s" % (name, exc), is_error=True))

    async def _single_turn(self, chat_id: int, text: str, **who: Any) -> None:
        async def progress(note: str) -> None:
            await self.listener.progress(chat_id, note)

        await self.listener.turn_started(chat_id)
        try:
            result = await self.ask(chat_id, text, on_progress=progress, **who)
        except asyncio.CancelledError:
            return  # stopped on purpose; whoever stopped it tells the user
        except Exception as exc:
            log.exception("turn failed for chat %s", chat_id)
            result = RunResult(text="Something went wrong: %s" % exc, is_error=True)
        await self.listener.result(chat_id, result)

    async def stop(self, chat_id: int) -> bool:
        """Stop whatever runs for a chat, including what it left in the background. True if it was working.

        The conversation itself is kept: the next message picks it up again.
        """
        was_working = False
        live = self._live.pop(chat_id, None)
        if live is not None:
            was_working = live.session.busy
            live.events.active = False
            await live.session.close()
        turns = list(self._turns.get(chat_id, ()))
        for turn in turns:
            turn.cancel()
        if turns:
            await asyncio.gather(*turns, return_exceptions=True)
        return was_working or bool(turns)

    async def shutdown(self) -> None:
        for chat_id in set(self._live) | set(self._turns):
            await self.stop(chat_id)

    async def wait_idle(self, chat_id: int) -> None:
        """Return once nothing is running for the chat and every result so far has been reported."""
        while True:
            turns = list(self._turns.get(chat_id, ()))
            live = self._live.get(chat_id)
            if turns:
                await asyncio.gather(*turns, return_exceptions=True)
            elif live is not None and live.session.busy:
                await live.session.wait_idle()
            else:
                return

    # -- one turn -----------------------------------------------------------

    def _prepare_workspace(self) -> None:
        """A bot Consiglieri created gets its manual written from its current settings before a process opens."""
        if self.config.bot_id:
            from . import bots

            bots.prepare_workspace(self.config)
        else:  # Consiglieri's main agent: only its copy of the shared memory's index changes between turns
            from . import shared_memory

            shared_memory.refresh_copy(self.config, self.config.home, shared_memory.MAIN)

    def _context(self, source: str, chat_id: int, user_id: Optional[int], username: Optional[str]) -> str:
        return context_line(source, chat_id, user_id, username, bot=self.bot_username)

    def _environment(self, chat_id: int, user_id: Optional[int], username: Optional[str]) -> Dict[str, str]:
        chat = Chat(chat_id)
        # Tokens stay with the runtime: `alfred send` looks them up by bot id, so the agent never needs them.
        env = {key: value for key, value in orca.detached_env(os.environ).items() if key != "TELEGRAM_BOT_TOKEN"}
        env["ALFRED_HOME"] = str(self.config.home)
        env["ALFRED_BOT_ID"] = str(self.config.bot_id)
        env["ALFRED_CHAT_ID"] = str(int(chat))
        env["ALFRED_THREAD_ID"] = str(chat.thread_id or "")
        workdir = self.config.workdir
        env["ALFRED_WORKDIR"] = str(workdir) if workdir else ""
        if workdir:
            # Claude Code then also reads the working directory's CLAUDE.md, as project instructions.
            env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "1"
        if self.config.bot_id:
            env["ALFRED_WORKSPACE"] = str(self.config.workspace)
        env["ALFRED_USER_ID"] = "" if user_id is None else str(user_id)
        env["ALFRED_USERNAME"] = username or ""
        # Consiglieri's launcher, then the base plugin's and each extension's bin/ (Claude Code adds those too).
        folders = [self.config.home / "bin"] + extensions.bin_dirs(self.config)
        env["PATH"] = os.pathsep.join([str(folder) for folder in folders] + [env.get("PATH", "")])
        return env

    async def ask(
        self,
        chat_id: int,
        text: str,
        *,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        source: str = "telegram",
        on_progress: Optional[ProgressCallback] = None,
    ) -> RunResult:
        """Run one turn for a chat. Turns for the same chat are serialized."""
        chat_id = Chat(chat_id)
        async with self._locks[chat_id]:
            name = self.backend_name(chat_id)
            backend = self._backend(name)
            session_id = self.sessions.session(chat_id, name)

            self._prepare_workspace()
            parts = [self._context(source, chat_id, user_id, username)]
            if not backend.native_skills and not session_id:
                index = home.skills_index(self.config)
                if index:
                    parts.append(index)
            parts.append(text)

            before = self._snapshot()
            try:
                result = await backend.run(
                    "\n\n".join(parts),
                    session_id=session_id,
                    cwd=self.config.workspace,
                    env=self._environment(chat_id, user_id, username),
                    on_progress=on_progress,
                )
            except BackendError as exc:
                return RunResult(text="%s backend failed: %s" % (name, exc), is_error=True)

            if result.session_id and result.session_id != session_id:
                self.sessions.set_session(chat_id, name, result.session_id)

            self._note_changes(result, before)
            await self._autosave(chat_id, text)
            return result
