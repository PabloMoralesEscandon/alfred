"""A long-lived Claude Code process for one chat.

`claude --print --input-format stream-json` stays open and reads one JSON
message per line. It sleeps between turns, wakes when a line arrives, and takes
a message that arrives mid-turn into the running turn at the next step. It also
starts a turn by itself when one of its own background tasks finishes. So
messages and results are not paired: whoever owns the session is told about
turn starts, tool use and results through `LiveEvents`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .base import STREAM_LIMIT, BackendError, RunResult

log = logging.getLogger("alfred.live")

KILL_GRACE_SECONDS = 5.0


class LiveEvents:
    """What a live session reports. Subclass and override what you need."""

    async def turn_started(self) -> None:
        """Work began: a message was handed over, or the process woke up by itself. May repeat."""

    async def progress(self, note: str) -> None:
        """The agent started using a tool."""

    async def session_started(self, session_id: str) -> None:
        """The id to resume this conversation with is known."""

    async def result(self, result: RunResult) -> None:
        """A turn ended. One result can answer several messages."""


class LiveSession:
    def __init__(
        self,
        command_for: Callable[[Optional[str]], List[str]],
        *,
        cwd: Path,
        env: Dict[str, str],
        session_id: Optional[str],
        events: LiveEvents,
        silence_timeout: float,
        describe_tool: Callable[[Dict[str, Any]], str] = lambda block: str(block.get("name") or "tool"),
    ) -> None:
        self._command_for = command_for
        self._cwd = cwd
        self._env = env
        self.session_id = session_id
        self.model: Optional[str] = None  # what the process reports it is running, once it has started a turn
        self._events = events
        self._silence_timeout = silence_timeout
        self._describe_tool = describe_tool

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional["asyncio.Future[None]"] = None
        self._watchdog: Optional["asyncio.Future[None]"] = None
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()

        self._pending: Dict[str, str] = {}  # message id -> text, until the process reports it handled
        self._in_turn = False
        self._started = False  # this process produced a turn, so its session exists
        self._resumed = False  # this process was started with --resume
        self._restarted = False  # the stored session was gone; say so on the next result
        self._tracks_messages = False  # this CLI version reports per-message progress
        self._last_text = ""
        self._last_activity = time.monotonic()
        self._closing = False

    # -- state --------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self.alive and self._process else None

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def _settle(self) -> None:
        if not self._in_turn and not self._pending:
            self._idle.set()

    # -- sending ------------------------------------------------------------

    async def send(self, text: str) -> None:
        """Hand a message to the agent. Returns as soon as it is written, not when it is answered."""
        async with self._lock:
            if self._closing:
                raise BackendError("the session is closed")
            if not self.alive:
                await self._spawn(self.session_id)
            message_id = str(uuid.uuid4())
            self._pending[message_id] = text
            was_idle = self._idle.is_set()
            self._idle.clear()
            self._last_activity = time.monotonic()
            await self._write(message_id, text)
        if was_idle:
            await self._events.turn_started()

    async def _write(self, message_id: str, text: str) -> None:
        assert self._process is not None and self._process.stdin is not None
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}, "uuid": message_id})
        try:
            self._process.stdin.write(line.encode("utf-8") + b"\n")
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the process is going away; the reader notices and decides what to do with the message

    async def _spawn(self, session_id: Optional[str]) -> None:
        command = self._command_for(session_id)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
                env=self._env,
                limit=STREAM_LIMIT,
                start_new_session=True,  # its own process group, so stopping it takes its children along
            )
        except FileNotFoundError as exc:
            raise BackendError("`%s` is not installed or not on PATH" % command[0]) from exc
        self._process = process
        self._resumed = bool(session_id)
        self._started = False
        self._in_turn = False
        self._tracks_messages = False
        self._last_text = ""
        self._reader = asyncio.ensure_future(self._read(process))
        if self._watchdog is None:
            self._watchdog = asyncio.ensure_future(self._watch())

    # -- reading ------------------------------------------------------------

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None and process.stderr is not None
        stderr_task = asyncio.ensure_future(process.stderr.read())
        try:
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._last_activity = time.monotonic()
                try:
                    await self._handle(event)
                except Exception:  # a failing listener must not leave the session deaf
                    log.exception("handling a %s event failed", event.get("type"))
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            await self._exited(process, await process.wait(), stderr)
        finally:
            stderr_task.cancel()

    async def _handle(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "command_lifecycle":
            self._tracks_messages = True
            # Anything but these two is an end state: "completed", or "cancelled" when the turn failed.
            if event.get("state") not in ("queued", "started"):
                self._pending.pop(str(event.get("command_uuid")), None)
                self._settle()
        elif kind == "system" and event.get("subtype") == "init":
            self._started = True
            self.model = event.get("model") or self.model
            session_id = event.get("session_id")
            if session_id and session_id != self.session_id:
                self.session_id = session_id
                await self._events.session_started(session_id)
            await self._begin_turn()
        elif kind == "assistant":
            await self._begin_turn()
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self._last_text = block["text"].strip()
                elif block.get("type") == "tool_use":
                    await self._events.progress(self._describe_tool(block))
        elif kind == "result":
            await self._finish_turn(event)

    async def _begin_turn(self) -> None:
        if self._in_turn:
            return
        self._in_turn = True
        self._idle.clear()
        await self._events.turn_started()

    async def _finish_turn(self, event: Dict[str, Any]) -> None:
        is_error = bool(event.get("is_error"))
        if is_error and self._resumed and not self._started:
            return  # the session to resume is gone; the process exits next and `_exited` starts a fresh one
        text = str(event.get("result") or "").strip() or self._last_text
        if not text:
            text = "Stopped without a reply (%s)." % event.get("subtype", "unknown") if is_error else ""
        result = RunResult(
            text=text,
            session_id=event.get("session_id") or self.session_id,
            is_error=is_error,
            cost_usd=event.get("total_cost_usd"),
            session_restarted=self._restarted,
        )
        self._restarted = False
        self._last_text = ""
        self._in_turn = False
        if not self._tracks_messages:
            self._pending.clear()  # no per-message reports from this CLI: the result answers what was sent
        try:
            await self._events.result(result)
        finally:
            self._settle()

    async def _exited(self, process: asyncio.subprocess.Process, returncode: int, stderr: str) -> None:
        if process is not self._process or self._closing:
            return
        waiting = list(self._pending.values())
        if self._resumed and not self._started and waiting:
            # Claude Code no longer has that conversation (pruned, moved, upgraded): start a new one.
            async with self._lock:
                self.session_id = None
                self._restarted = True
                self._pending.clear()
                try:
                    await self._spawn(None)
                except BackendError as exc:
                    await self._fail(str(exc))
                    return
                for text in waiting:
                    message_id = str(uuid.uuid4())
                    self._pending[message_id] = text
                    await self._write(message_id, text)
            return
        if waiting or self._in_turn:
            detail = stderr.splitlines()[-1] if stderr else "exit code %d" % returncode
            await self._fail("claude stopped before finishing (%s)" % detail)
            return
        self._idle.set()  # it went away while idle; the next message starts it again

    async def _fail(self, message: str) -> None:
        self._pending.clear()
        self._in_turn = False
        self._restarted = False
        try:
            await self._events.result(RunResult(text=message, session_id=self.session_id, is_error=True))
        finally:
            self._idle.set()

    async def _watch(self) -> None:
        """A turn that produces no output at all for too long is stuck: stop it and say so."""
        while True:
            await asyncio.sleep(min(5.0, max(0.05, self._silence_timeout / 4)))
            if self.busy and self.alive and time.monotonic() - self._last_activity > self._silence_timeout:
                process = self._process
                self._process = None  # so the reader does not also report this exit
                if process is not None:
                    await self._kill(process)
                await self._fail("no sign of life for %d seconds, so I stopped it" % self._silence_timeout)

    # -- stopping -----------------------------------------------------------

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await process.wait()

    async def close(self) -> None:
        """Stop the process and whatever it started. The conversation can be resumed by a new session."""
        self._closing = True
        if self._watchdog is not None:
            self._watchdog.cancel()
        process = self._process
        if process is not None:
            await self._kill(process)
        if self._reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._reader), timeout=KILL_GRACE_SECONDS)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._reader.cancel()
        self._pending.clear()
        self._in_turn = False
        self._idle.set()
