"""Shared machinery for agent backends: a CLI that takes a prompt and streams JSON lines."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

ProgressCallback = Callable[[str], Awaitable[None]]

# A single stream-json line can carry a whole file; the asyncio default (64 KiB) is too small.
STREAM_LIMIT = 64 * 1024 * 1024


class BackendError(RuntimeError):
    pass


@dataclass
class RunResult:
    text: str
    session_id: Optional[str] = None
    is_error: bool = False
    cost_usd: Optional[float] = None
    new_skills: List[str] = field(default_factory=list)
    # Notes such as "saved people/sam" for memory files this turn added, changed or removed.
    memory_changes: List[str] = field(default_factory=list)
    extension_changes: List[str] = field(default_factory=list)
    # The backend could not resume the session it was given; nothing was run.
    session_lost: bool = False
    # The stored session was gone, so this turn ran in a brand-new conversation.
    session_restarted: bool = False


@dataclass
class ProcessOutput:
    returncode: int
    stderr: str


def shorten(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def run_json_lines(
    command: List[str],
    *,
    stdin_text: str,
    cwd: Path,
    env: Dict[str, str],
    on_event: Callable[[Dict[str, Any]], Awaitable[None]],
    timeout: float,
) -> ProcessOutput:
    """Run `command`, feed it `stdin_text`, and call `on_event` for every JSON line on stdout.

    The child gets its own process group so a timeout or cancellation (the
    user's /stop) takes down the agent and anything it spawned.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            limit=STREAM_LIMIT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise BackendError("`%s` is not installed or not on PATH" % command[0]) from exc

    async def pump() -> ProcessOutput:
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(stdin_text.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        stderr_task = asyncio.ensure_future(process.stderr.read())
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            await on_event(event)
        stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
        return ProcessOutput(returncode=await process.wait(), stderr=stderr)

    try:
        return await asyncio.wait_for(pump(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise BackendError("timed out after %d seconds" % timeout) from exc
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


class Backend:
    """One agent CLI. Subclasses implement `_run_once`."""

    name = "base"
    # True when the CLI discovers `.claude/skills` by itself. Otherwise Consiglieri
    # puts a skills index into the first prompt of each session.
    native_skills = False
    # True when the CLI can stay open between messages (see `live.LiveSession`).
    supports_live = False

    def __init__(self, config: Any) -> None:
        self.config = config

    def live_settings(self) -> Any:
        """Everything a stay-open process is started with. A change means it has to be reopened."""
        return None

    async def run(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        cwd: Path,
        env: Dict[str, str],
        on_progress: Optional[ProgressCallback] = None,
    ) -> RunResult:
        result = await self._run_once(prompt, session_id=session_id, cwd=cwd, env=env, on_progress=on_progress)
        if session_id and result.session_lost:
            # The stored session is gone (pruned, other machine, CLI upgrade): start over.
            result = await self._run_once(prompt, session_id=None, cwd=cwd, env=env, on_progress=on_progress)
            result.session_restarted = True
        return result

    async def _run_once(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        cwd: Path,
        env: Dict[str, str],
        on_progress: Optional[ProgressCallback],
    ) -> RunResult:
        raise NotImplementedError
