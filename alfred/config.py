"""Configuration: environment variables, with `.env` files as a fallback.

Lookup order for every setting: process environment, then `$ALFRED_HOME/.env`,
then the `.env` next to this repository. The allow-list is re-read on every
check so access can be changed without restarting the bot.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = Path.home() / ".alfred"
LEGACY_WHITELIST_PREFIX = "whitelisted@"


def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _parse_env_lines(lines: List[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _legacy_whitelist(lines: List[str]) -> Set[str]:
    """Old syntax: bare `whitelisted@username` tokens anywhere in `.env`."""
    names: Set[str] = set()
    for raw in lines:
        line = raw.split("#", 1)[0]
        for token in line.replace(",", " ").split():
            if token.startswith(LEGACY_WHITELIST_PREFIX):
                name = token[len(LEGACY_WHITELIST_PREFIX):].lstrip("@").strip()
                if name:
                    names.add(name.lower())
    return names


@dataclass
class Config:
    home: Path = DEFAULT_HOME
    env_files: List[Path] = field(default_factory=list)
    environ: Dict[str, str] = field(default_factory=lambda: dict(os.environ))

    @classmethod
    def load(cls, environ: Optional[Dict[str, str]] = None) -> "Config":
        environ = dict(os.environ if environ is None else environ)
        # ALFRED_ENV_FILE points at a different settings file than the one next to the code.
        repo_env = Path(environ.get("ALFRED_ENV_FILE") or REPO_ROOT / ".env").expanduser()
        home_text = environ.get("ALFRED_HOME") or _parse_env_lines(_read_lines(repo_env)).get("ALFRED_HOME")
        home = Path(home_text).expanduser().resolve() if home_text else DEFAULT_HOME
        return cls(home=home, env_files=[home / ".env", repo_env], environ=environ)

    # -- raw lookup ---------------------------------------------------------

    def _file_lines(self) -> List[str]:
        lines: List[str] = []
        for path in self.env_files:
            lines.extend(_read_lines(path))
        return lines

    def get(self, name: str, default: str = "") -> str:
        value = self.environ.get(name, "").strip()
        if value:
            return value
        for path in self.env_files:
            value = _parse_env_lines(_read_lines(path)).get(name, "")
            if value:
                return value
        return default

    def get_bool(self, name: str, default: bool) -> bool:
        value = self.get(name).lower()
        if not value:
            return default
        return value in ("1", "true", "yes", "on")

    # -- settings -----------------------------------------------------------

    @property
    def telegram_token(self) -> str:
        token = self.get("TELEGRAM_BOT_TOKEN")
        if not token or token == "replace_me":
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set. Put it in %s or %s."
                % (self.env_files[0], self.env_files[-1])
            )
        return token

    @property
    def default_backend(self) -> str:
        return self.get("ALFRED_BACKEND", "claude").lower()

    @property
    def claude_bin(self) -> str:
        return self.get("ALFRED_CLAUDE_BIN", "claude")

    @property
    def claude_model(self) -> str:
        return self.get("ALFRED_CLAUDE_MODEL")

    @property
    def claude_effort(self) -> str:
        """How hard Claude thinks: low, medium, high, xhigh or max. Unset means Claude Code's own setting."""
        return self.get("ALFRED_CLAUDE_EFFORT").lower()

    @property
    def claude_permission_mode(self) -> str:
        return self.get("ALFRED_CLAUDE_PERMISSION_MODE", "bypassPermissions")

    @property
    def claude_extra_args(self) -> List[str]:
        """Extra flags for every `claude` call, for example `--chrome` to use Claude in Chrome."""
        return shlex.split(self.get("ALFRED_CLAUDE_EXTRA_ARGS"))

    @property
    def browser(self) -> str:
        """Which browser the agent drives: `orca` (default), `chrome` (Claude in Chrome), or `none`."""
        value = self.get("ALFRED_BROWSER", "orca").lower()
        return value if value in ("orca", "chrome", "none") else "orca"

    @property
    def chrome_profile(self) -> str:
        """The Chrome profile folder kept for Alfred (for example `Alfred`), so `alfred chrome` can open it."""
        return self.get("ALFRED_CHROME_PROFILE")

    @property
    def chrome_app(self) -> str:
        """The browser `alfred chrome` opens: an app name on macOS, a command elsewhere."""
        return self.get("ALFRED_CHROME_APP", "Google Chrome" if sys.platform == "darwin" else "google-chrome")

    @property
    def orca_bin(self) -> str:
        return self.get("ALFRED_ORCA_BIN", "orca")

    @property
    def codex_bin(self) -> str:
        return self.get("ALFRED_CODEX_BIN", "codex")

    @property
    def codex_model(self) -> str:
        return self.get("ALFRED_CODEX_MODEL")

    @property
    def codex_effort(self) -> str:
        """Codex's `model_reasoning_effort` (for example low, medium, high). Unset means Codex's own setting."""
        return self.get("ALFRED_CODEX_EFFORT").lower()

    @property
    def codex_sandbox(self) -> str:
        return self.get("ALFRED_CODEX_SANDBOX", "danger-full-access")

    @property
    def live(self) -> bool:
        """Keep one agent process open per chat instead of starting one for every message."""
        return self.get_bool("ALFRED_LIVE", True)

    @property
    def timeout_seconds(self) -> float:
        """How long a turn may go without producing anything before it is stopped.

        For a backend that starts one process per message, the limit for the whole turn.
        """
        return float(self.get("ALFRED_TIMEOUT", "900"))

    @property
    def autosave(self) -> bool:
        """Commit the home directory to its own git repo after each turn."""
        return self.get_bool("ALFRED_AUTOSAVE", True)

    @property
    def show_progress(self) -> bool:
        return self.get_bool("ALFRED_PROGRESS", True)

    # -- paths --------------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "scheduler.sqlite3"

    @property
    def sessions_path(self) -> Path:
        return self.state_dir / "sessions.json"

    @property
    def skills_dir(self) -> Path:
        return self.home / ".claude" / "skills"

    @property
    def extensions_dir(self) -> Path:
        """Alfred's own additions, one Claude Code plugin per folder. Loaded into every process."""
        return self.home / "extensions"

    @property
    def disabled_extensions_dir(self) -> Path:
        return self.home / "extensions-disabled"

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def memory_index(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    @property
    def inbox_dir(self) -> Path:
        return self.home / "inbox"

    # -- access control -----------------------------------------------------

    def allowed_users(self) -> Set[str]:
        """Lower-cased usernames and numeric user ids allowed to talk to Alfred."""
        allowed = _legacy_whitelist(self._file_lines())
        for entry in self.get("ALFRED_ALLOWED_USERS").replace(";", ",").split(","):
            entry = entry.strip().lstrip("@").lower()
            if entry:
                allowed.add(entry)
        return allowed

    def is_allowed(self, user_id: Optional[int], username: Optional[str]) -> bool:
        allowed = self.allowed_users()
        if user_id is not None and str(user_id) in allowed:
            return True
        return bool(username) and username.lstrip("@").lower() in allowed
