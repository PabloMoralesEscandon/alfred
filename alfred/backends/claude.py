"""Claude Code backend, run in the home directory with one resumable session per chat.

Two ways to run it: `open_live` keeps one process per chat open (what the bot
uses), and `run` starts a process for a single turn (the terminal chat).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import extensions
from .base import Backend, ProgressCallback, RunResult, run_json_lines, shorten
from .live import LiveEvents, LiveSession

# Set by a parent Claude Code session. Alfred's child must not think it is nested in one.
INHERITED_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)

# Which input field best describes a tool call in a one-line progress note.
TOOL_SUMMARY_KEYS = ("description", "command", "skill", "file_path", "pattern", "query", "url", "prompt")


def describe_tool_use(block: Dict[str, Any]) -> str:
    name = str(block.get("name") or "tool")
    tool_input = block.get("input") or {}
    if name.startswith("mcp__"):  # mcp__claude-in-chrome__navigate -> claude-in-chrome navigate
        name = " ".join(part for part in name.split("__")[1:] if part)
        if tool_input.get("action"):
            name = "%s %s" % (name, tool_input["action"])
    if name == "Skill" and tool_input.get("skill"):
        return "Using skill: %s" % tool_input["skill"]
    for key in TOOL_SUMMARY_KEYS:
        value = tool_input.get(key)
        if value:
            if key == "file_path":
                value = Path(str(value)).name
            return "%s: %s" % (name, shorten(value))
    return name


class ClaudeBackend(Backend):
    name = "claude"
    native_skills = True

    supports_live = True

    def command(self, session_id: Optional[str], live: bool = False) -> List[str]:
        config = self.config
        command = [config.claude_bin, "--print"]
        if live:
            command += ["--input-format", "stream-json"]  # stay open and read one message per line
        command += [
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", config.claude_permission_mode,
        ]
        if config.claude_model:
            command += ["--model", config.claude_model]
        if config.claude_effort:
            command += ["--effort", config.claude_effort]
        if session_id:
            command += ["--resume", session_id]
        if config.browser == "chrome" and "--chrome" not in config.claude_extra_args:
            command.append("--chrome")
        for plugin in extensions.plugin_dirs(config):  # the base plugin, then each enabled extension
            command += ["--plugin-dir", str(plugin)]
        return command + config.claude_extra_args

    def live_settings(self) -> List[str]:
        # An extension that was added, removed or reshaped needs a new process to load.
        return self.command(None, live=True) + ["#extensions=%s" % extensions.fingerprint(self.config)]

    def open_live(
        self, *, session_id: Optional[str], cwd: Path, env: Dict[str, str], events: LiveEvents
    ) -> LiveSession:
        """A session whose process stays open between messages. Nothing starts until the first message."""
        return LiveSession(
            lambda resume: self.command(resume, live=True),
            cwd=cwd,
            env={key: value for key, value in env.items() if key not in INHERITED_SESSION_VARS},
            session_id=session_id,
            events=events,
            silence_timeout=self.config.timeout_seconds,
            describe_tool=describe_tool_use,
        )

    async def _run_once(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        cwd: Path,
        env: Dict[str, str],
        on_progress: Optional[ProgressCallback],
    ) -> RunResult:
        env = {key: value for key, value in env.items() if key not in INHERITED_SESSION_VARS}
        state: Dict[str, Any] = {"result": None, "started": False, "last_text": ""}

        async def on_event(event: Dict[str, Any]) -> None:
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                state["started"] = True
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        state["last_text"] = block["text"].strip()
                    elif block.get("type") == "tool_use" and on_progress is not None:
                        await on_progress(describe_tool_use(block))
            elif kind == "result":
                state["result"] = event

        output = await run_json_lines(
            self.command(session_id),
            stdin_text=prompt,
            cwd=cwd,
            env=env,
            on_event=on_event,
            timeout=self.config.timeout_seconds,
        )

        result = state["result"]
        if result is None:
            detail = output.stderr or "claude exited with code %d and no result" % output.returncode
            return RunResult(text=detail, is_error=True, session_lost=bool(session_id) and not state["started"])

        is_error = bool(result.get("is_error"))
        if is_error and session_id and not state["started"]:
            return RunResult(text=output.stderr or "session not found", is_error=True, session_lost=True)

        text = str(result.get("result") or "").strip() or state["last_text"]
        if not text:
            text = "Stopped without a reply (%s)." % result.get("subtype", "unknown") if is_error else "Done."
        return RunResult(
            text=text,
            session_id=result.get("session_id") or session_id,
            is_error=is_error,
            cost_usd=result.get("total_cost_usd"),
        )
