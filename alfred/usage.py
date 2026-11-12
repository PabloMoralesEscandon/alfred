"""`/usage`: a short version of what Claude Code's own `/usage` screen shows.

Claude Code answers `/usage` in headless mode without calling the model, so
this costs nothing and reflects the account's real limits.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .backends.base import BackendError, run_json_lines
from .backends.claude import INHERITED_SESSION_VARS
from .config import Config

USAGE_TIMEOUT = 45.0
# "Current week (all models): 15% used · resets Nov 15 at 5:59am (Europe/Berlin)"
LIMIT_LINE = re.compile(r"^Current (?P<label>[^:]+):\s*(?P<percent>\d+)% used(?:\s*·\s*resets (?P<reset>.+?))?(?:\s*\([^)]*\))?\s*$")
BAR_WIDTH = 10


def bar(percent: int) -> str:
    filled = max(0, min(BAR_WIDTH, int(percent * BAR_WIDTH / 100 + 0.5)))  # round half up
    return "▓" * filled + "░" * (BAR_WIDTH - filled)


def summarize(report: str) -> str:
    """Reduce Claude Code's usage report to one line per limit."""
    lines: List[str] = []
    for raw in report.splitlines():
        match = LIMIT_LINE.match(raw.strip())
        if not match:
            continue
        label = match.group("label").strip()
        label = label[0].upper() + label[1:]
        percent = int(match.group("percent"))
        line = "%s: %d%% used\n%s" % (label, percent, bar(percent))
        if match.group("reset"):
            line += " resets %s" % match.group("reset").strip()
        lines.append(line)
    if lines:
        return "Claude usage\n\n" + "\n\n".join(lines)
    # Accounts billed by API key, or a future format: show the first paragraph as it came.
    first = report.strip().split("\n\n", 1)[0].strip()
    return first or "Claude Code returned no usage information."


async def fetch(config: Config, environ: Dict[str, str]) -> str:
    """Ask Claude Code for the usage report and return the short version."""
    state: Dict[str, Optional[Dict[str, Any]]] = {"result": None}

    async def on_event(event: Dict[str, Any]) -> None:
        if event.get("type") == "result" or "result" in event:
            state["result"] = event

    env = {key: value for key, value in environ.items() if key not in INHERITED_SESSION_VARS}
    try:
        output = await run_json_lines(
            [config.claude_bin, "--print", "--output-format", "json", "--no-session-persistence"],
            stdin_text="/usage",
            cwd=config.home,
            env=env,
            on_event=on_event,
            timeout=USAGE_TIMEOUT,
        )
    except BackendError as exc:
        return "Could not get usage: %s" % exc
    result = state["result"]
    if result is None or result.get("is_error"):
        detail = str((result or {}).get("result") or output.stderr or "no answer from claude").strip()
        return "Could not get usage: %s" % detail.splitlines()[0]
    return summarize(str(result.get("result") or ""))
