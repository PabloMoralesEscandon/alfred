"""Orca integration: Alfred's browser is the one built into the Orca app, driven by the `orca` CLI.

Alfred does not run inside Orca. Its home is registered as an Orca workspace,
so browser commands run from the home open tabs there (visible in the app, with
the app's persistent logins) instead of in whichever workspace is on screen.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import Config

# Remote-runtime selection is the user's choice and is passed through. Everything else
# Orca exports into its terminals describes that terminal's pane and workspace, and
# would make Alfred's agent report into, and open tabs in, the wrong place.
KEPT_ORCA_VARS = ("ORCA_PAIRING_CODE", "ORCA_ENVIRONMENT", "ORCA_CLI_COMMAND")
TEST_URL = "https://example.com/"

Step = Tuple[str, bool, str]


class OrcaError(RuntimeError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def detached_env(env: Mapping[str, str]) -> Dict[str, str]:
    """Copy of `env` without the context of the Orca terminal the bot was started from."""
    return {key: value for key, value in env.items() if not key.startswith("ORCA_") or key in KEPT_ORCA_VARS}


def call(config: Config, *args: str, timeout: float = 60) -> Dict[str, Any]:
    """Run an `orca` command from Alfred's home and return its `result` object."""
    try:
        process = subprocess.run(
            [config.orca_bin, *args, "--json"],
            cwd=str(config.home),
            env=detached_env(config.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise OrcaError("`%s` is not installed or not on PATH" % config.orca_bin, "cli_missing") from None
    except subprocess.TimeoutExpired:
        raise OrcaError("`orca %s` timed out" % " ".join(args), "timeout") from None
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        detail = (process.stderr or process.stdout).strip().splitlines()
        raise OrcaError(detail[-1] if detail else "no output from `orca %s`" % " ".join(args)) from None
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise OrcaError(str(error.get("message") or error.get("code") or "failed"), str(error.get("code") or ""))
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def runtime_ready(config: Config) -> bool:
    try:
        return bool(call(config, "status", timeout=20).get("runtime", {}).get("reachable"))
    except OrcaError:
        return False


def workspace(config: Config) -> Optional[Dict[str, Any]]:
    """The Orca workspace that contains Alfred's home, if it has been registered."""
    try:
        found = call(config, "worktree", "current").get("worktree")
    except OrcaError as exc:
        if exc.code == "selector_not_found":
            return None
        raise
    return found if isinstance(found, dict) else None


def summary(config: Config) -> str:
    """One line for logs and /status. Never raises."""
    if config.browser == "none":
        return "off"
    if config.browser == "chrome":
        # Whether the extension is connected is only knowable from inside a Claude turn.
        if config.chrome_profile:
            return "Claude in Chrome, profile %s (`alfred chrome` opens it)" % config.chrome_profile
        return "Claude in Chrome (Chrome must be open with the Claude extension signed in)"
    if not shutil.which(config.orca_bin):
        return "unavailable (Orca CLI not found)"
    if not runtime_ready(config):
        return "unavailable (Orca is not running; `orca open` starts it)"
    try:
        linked = workspace(config) is not None
    except OrcaError as exc:
        return "unavailable (%s)" % exc
    return "ready" if linked else "works, but not linked to its own workspace (run `alfred browser-setup`)"


def setup_browser(config: Config, *, test: bool = True) -> List[Step]:
    """Make the browser usable from Alfred's home. Idempotent. Returns (step, ok, detail) rows."""
    steps: List[Step] = []

    def record(label: str, ok: bool, detail: str) -> bool:
        steps.append((label, ok, detail))
        return ok

    located = shutil.which(config.orca_bin)
    if not record("Orca CLI", bool(located), located or "not found; install Orca or set ALFRED_ORCA_BIN"):
        return steps

    if not runtime_ready(config):
        try:
            call(config, "open", timeout=120)
        except OrcaError as exc:
            record("Orca runtime", False, "could not start Orca: %s" % exc)
            return steps
    ready = runtime_ready(config)
    if not record("Orca runtime", ready, "reachable" if ready else "not reachable after `orca open`"):
        return steps

    try:
        found = workspace(config)
        if found is None:
            call(config, "repo", "add", "--path", str(config.home))
            found = workspace(config)
            detail = "registered %s with Orca" % config.home
        else:
            detail = "already registered"
        if not record("Workspace", found is not None, detail if found else "Orca did not pick up %s" % config.home):
            return steps
    except OrcaError as exc:
        record("Workspace", False, str(exc))
        return steps

    if test:
        page: Optional[str] = None
        try:
            page = str(call(config, "tab", "create", "--url", TEST_URL)["browserPageId"])
            tabs = call(config, "tab", "list").get("tabs", [])
            mine = [tab for tab in tabs if tab.get("browserPageId") == page]
            scoped = bool(mine) and Path(str(mine[0].get("worktreeId", "")).split("::")[-1]) == config.home
            call(config, "wait", "--load", "networkidle", "--page", page)
            title = str(call(config, "get", "--what", "title", "--page", page).get("title") or "")
            record("Page load", bool(title), "opened %s: %r" % (TEST_URL, title))
            record("Tab scoping", scoped, "tabs open in Consiglieri's workspace" if scoped else "tab opened in another workspace")
        except (OrcaError, KeyError) as exc:
            record("Page load", False, str(exc))
        finally:
            if page:
                try:
                    call(config, "tab", "close", "--page", page)
                except OrcaError:
                    pass
    return steps


def screenshot(config: Config, target: Path, *, page: Optional[str] = None, full_page: bool = False) -> Path:
    """Save a screenshot of a browser tab as a file. Orca returns the image inline, which is too big to print."""
    import base64

    arguments = ["full-screenshot" if full_page else "screenshot", "--format", "png"]
    if page:
        arguments += ["--page", page]
    data = call(config, *arguments).get("data")
    if not data:
        raise OrcaError("Orca returned no image")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(data))
    return target
