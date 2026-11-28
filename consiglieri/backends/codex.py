"""Codex backend: `codex exec --json`, kept as an opt-in alternative to Claude Code."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Backend, ProgressCallback, RunResult, run_json_lines, shorten


class CodexBackend(Backend):
    name = "codex"
    native_skills = False

    def command(self, session_id: Optional[str]) -> List[str]:
        config = self.config
        command = [config.codex_bin, "exec"]
        if session_id:
            command.append("resume")
        command += ["--json", "--skip-git-repo-check"]
        if config.codex_sandbox == "danger-full-access":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            # `exec resume` has no --sandbox flag; the config override works for both forms.
            command += ["-c", 'sandbox_mode="%s"' % config.codex_sandbox]
        if config.codex_model:
            command += ["--model", config.codex_model]
        if config.codex_effort:
            command += ["-c", 'model_reasoning_effort="%s"' % config.codex_effort]
        if session_id:
            command.append(session_id)
        command.append("-")  # prompt on stdin
        return command

    async def _run_once(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        cwd: Path,
        env: Dict[str, str],
        on_progress: Optional[ProgressCallback],
    ) -> RunResult:
        state: Dict[str, Any] = {"thread_id": None, "messages": [], "errors": [], "turn_started": False}

        async def on_event(event: Dict[str, Any]) -> None:
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "thread.started":
                state["thread_id"] = event.get("thread_id")
            elif kind == "turn.started":
                state["turn_started"] = True
            elif kind == "item.started" and item.get("type") == "command_execution" and on_progress is not None:
                await on_progress("Shell: %s" % shorten(item.get("command", "")))
            elif kind == "item.completed" and item.get("type") == "agent_message":
                if str(item.get("text") or "").strip():
                    state["messages"].append(item["text"].strip())
            elif kind == "turn.failed":
                state["errors"].append(str((event.get("error") or {}).get("message") or "turn failed"))
            elif kind == "error":
                state["errors"].append(str(event.get("message") or "error"))

        output = await run_json_lines(
            self.command(session_id),
            stdin_text=prompt,
            cwd=cwd,
            env=env,
            on_event=on_event,
            timeout=self.config.timeout_seconds,
        )

        thread_id = state["thread_id"] or session_id
        if state["messages"]:
            return RunResult(text=state["messages"][-1], session_id=thread_id)

        detail = state["errors"][-1] if state["errors"] else output.stderr
        detail = detail or "codex exited with code %d and no reply" % output.returncode
        session_lost = bool(session_id) and not state["turn_started"]
        return RunResult(text=detail, session_id=None if session_lost else thread_id, is_error=True, session_lost=session_lost)
