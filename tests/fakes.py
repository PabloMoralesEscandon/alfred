"""Stand-ins for the `claude` and `codex` CLIs: scripts that replay canned JSON lines."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from alfred.config import Config

FAKE_CLI = """#!{python}
import json, os, subprocess, sys, threading, time
argv = sys.argv[1:]
LOG, SCRIPT = {log!r}, {script!r}
resuming = "--resume" in argv or "resume" in argv


def record(entry):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(entry) + "\\n")


def out(line):
    print(json.dumps(line), flush=True)


if "--input-format" not in argv:  # one process per turn: the prompt is all of stdin
    record({{"argv": argv, "stdin": sys.stdin.read()}})
    script = json.load(open(SCRIPT))
    for line in script["resume" if resuming and "resume" in script else "fresh"]:
        out(line)
    sys.exit(script.get("resume_exit" if resuming else "exit", 0))

# A process that stays open: one JSON message per line of stdin, answered from the script each time.
with open(LOG + ".pid", "w") as handle:
    handle.write(str(os.getpid()))
script = json.load(open(SCRIPT))
if resuming and script.get("resume_exit"):  # the session to resume does not exist
    for line in script.get("resume", []):
        out(line)
    sys.exit(script["resume_exit"])


def lifecycle(message, state):
    if json.load(open(SCRIPT)).get("lifecycle", True):
        out({{"type": "command_lifecycle", "command_uuid": message.get("uuid"), "state": state}})


def take(raw):
    message = json.loads(raw)
    record({{"argv": argv, "stdin": message["message"]["content"], "live": True}})
    return message


for raw in sys.stdin:
    message = take(raw)
    lifecycle(message, "started")
    script = json.load(open(SCRIPT))
    for line in script["resume" if resuming and "resume" in script else "fresh"]:
        if "_sleep" in line:
            time.sleep(line["_sleep"])
        elif "_run" in line:
            subprocess.run(line["_run"], shell=True)
        elif "_exit" in line:
            os._exit(line["_exit"])
        elif "_absorb" in line:  # a message that arrives mid-turn joins the running turn
            lifecycle(take(sys.stdin.readline()), "completed")
        elif "_later" in line:  # the process starts a turn by itself, like a finished background task
            threading.Timer(line["_after"], lambda lines=line["_later"]: [out(item) for item in lines]).start()
        else:
            out(line)
    lifecycle(message, script.get("final_state", "completed"))
"""


class Sandbox:
    """A temporary home plus fake backends, wired into a Config."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name).resolve()
        self.home = self.root / "home"
        self.environ: Dict[str, str] = {"ALFRED_HOME": str(self.home), "ALFRED_AUTOSAVE": "0", "PATH": os.environ["PATH"]}
        # Tests never read the real .env next to the code, which may hold a live bot token.
        self.environ["ALFRED_ENV_FILE"] = str(self.root / "repo.env")
        # Tests never talk to a real Orca app; install_fake_orca() overrides this.
        self.environ["ALFRED_ORCA_BIN"] = str(self.root / "no-orca")
        self.config = Config(home=self.home, env_files=[self.home / ".env", self.root / "repo.env"], environ=self.environ)

    def cleanup(self) -> None:
        self._tempdir.cleanup()

    def fake_cli(self, name: str, script: Dict[str, Any]) -> Path:
        """Install a fake CLI. `script` maps "fresh"/"resume" to the JSON lines to print.

        A stay-open process re-reads the script for every message, so calling this again changes
        what the running process answers next. Lines with a `_` key are directions, not output:
        `_sleep`, `_run` (a shell command), `_exit`, `_absorb`, and `_later` with `_after`.
        """
        script_path = self.root / ("%s.json" % name)
        script_path.write_text(json.dumps(script), encoding="utf-8")
        executable = self.root / name
        executable.write_text(
            FAKE_CLI.format(python=sys.executable, log=str(self.root / ("%s.log" % name)), script=str(script_path)),
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        self.environ["ALFRED_%s_BIN" % name.upper()] = str(executable)
        return executable

    def live_pid(self, name: str) -> int:
        """The pid of the fake CLI's most recent stay-open process."""
        return int((self.root / ("%s.log.pid" % name)).read_text())

    def calls(self, name: str) -> List[Dict[str, Any]]:
        log = self.root / ("%s.log" % name)
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def claude_lines(text: str, session_id: str = "sess-1", tool: bool = False) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = [{"type": "system", "subtype": "init", "session_id": session_id}]
    if tool:
        lines.append(
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la", "description": "List files"}}
            ]}}
        )
    lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    lines.append(
        {"type": "result", "subtype": "success", "is_error": False, "result": text,
         "session_id": session_id, "total_cost_usd": 0.01}
    )
    return lines


CLAUDE_SESSION_GONE = [
    {"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0, "session_id": "old"}
]


FAKE_ORCA = """#!{python}
import base64, json, os, sys
argv = [a for a in sys.argv[1:] if a != "--json"]
state_path = {state!r}
state = json.load(open(state_path))
state["calls"].append({{"argv": argv, "cwd": os.getcwd(), "orca_env": sorted(k for k in os.environ if k.startswith("ORCA_"))}})

def reply(result=None, error=None):
    json.dump(state, open(state_path, "w"))
    print(json.dumps({{"ok": error is None, "result": result, "error": error}}))
    sys.exit(0 if error is None else 1)

command = " ".join(argv[:2])
home = os.path.realpath(os.getcwd())
if argv[0] == "status":
    reply({{"runtime": {{"reachable": state["running"]}}}})
if argv[0] == "open":
    state["running"] = True
    reply({{}})
if command == "worktree current":
    if home in state["registered"]:
        reply({{"worktree": {{"id": "repo::" + home, "path": home}}}})
    reply(error={{"code": "selector_not_found", "message": "No Orca-managed worktree contains " + home}})
if command == "repo add":
    state["registered"].append(os.path.realpath(argv[argv.index("--path") + 1]))
    reply({{}})
if command == "tab create":
    scope = home if home in state["registered"] else "/somewhere/else"
    state["tabs"].append({{"browserPageId": "page-1", "worktreeId": "repo::" + scope}})
    reply({{"browserPageId": "page-1"}})
if command == "tab list":
    reply({{"tabs": state["tabs"]}})
if command == "tab close":
    state["tabs"] = []
    reply({{"closed": True}})
if argv[0] == "wait":
    reply({{"waited": "load"}})
if argv[0] == "get":
    reply({{"title": "Example Domain"}})
if argv[0] in ("screenshot", "full-screenshot"):
    reply({{"format": "png", "data": base64.b64encode(b"PNG:" + argv[0].encode()).decode()}})
reply(error={{"code": "unknown", "message": "unexpected: " + " ".join(argv)}})
"""


def install_fake_orca(sandbox: "Sandbox", *, running: bool = True, registered: bool = False) -> Path:
    """A fake `orca` CLI with just enough state for browser setup and screenshots."""
    state = sandbox.root / "orca-state.json"
    homes = [os.path.realpath(str(sandbox.home))] if registered else []
    state.write_text(json.dumps({"running": running, "registered": homes, "tabs": [], "calls": []}), encoding="utf-8")
    executable = sandbox.root / "orca"
    executable.write_text(FAKE_ORCA.format(python=sys.executable, state=str(state)), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    sandbox.environ["ALFRED_ORCA_BIN"] = str(executable)
    return state
