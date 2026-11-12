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
from typing import Callable, Dict, List, Optional, Set

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
    # Set for one of the bots Consiglieri created (see `alfred/bots.py`); 0 is the main bot.
    bot_id: int = 0
    # Where this bot's agent runs and keeps its memory and inbox. None means the home itself.
    workspace_dir: Optional[Path] = None
    # Per-bot settings, as the same names as the environment (ALFRED_CLAUDE_MODEL, ...). Read on every lookup,
    # so a change applies from the next message, just like an edit to .env. An unset one falls back to .env.
    overrides: Optional[Callable[[], Dict[str, str]]] = None

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
        if self.overrides is not None:
            value = str(self.overrides().get(name) or "").strip()
            if value:
                return value
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
        """The Chrome profile folder kept for Consiglieri (for example `Alfred`), so `alfred chrome` can open it."""
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

    def _bot_value(self, name: str) -> str:
        """A setting only a created bot has. It comes from the bot's own settings, never from Consiglieri's .env."""
        if not self.bot_id or self.overrides is None:
            return ""
        return str(self.overrides().get(name) or "").strip()

    @property
    def workdir(self) -> Optional[Path]:
        """The directory a bot does its job in, besides its own. None: it works in its own directory."""
        value = self._bot_value("ALFRED_WORKDIR")
        return Path(value).expanduser() if value else None

    @property
    def add_dirs(self) -> List[Path]:
        """Directories handed to Claude Code with --add-dir: a bot's working directory and extra folders."""
        value = self._bot_value("ALFRED_ADD_DIRS")
        return [Path(part) for part in value.split(os.pathsep) if part]

    @property
    def claude_settings_file(self) -> Optional[Path]:
        """A bot's generated Claude Code settings (its permissions), passed with --settings."""
        value = self._bot_value("ALFRED_CLAUDE_SETTINGS")
        return Path(value) if value else None

    # -- paths --------------------------------------------------------------

    @property
    def workspace(self) -> Path:
        """The agent's own directory: where it runs, remembers and receives files. The home for the main bot."""
        return self.workspace_dir or self.home

    @property
    def bots_state_dir(self) -> Path:
        """Settings, sessions and tokens of the bots Consiglieri created, one folder each. Never committed."""
        return self.state_dir / "bots"

    @property
    def bots_home(self) -> Path:
        """Where the bots' own directories live: next to the home, never inside it.

        Claude Code loads every CLAUDE.md from its working directory up to the root, so a bot running
        inside the home would also load the main agent's manual and memory index.
        """
        value = self.get("ALFRED_BOTS_HOME")
        if value:
            return Path(value).expanduser().resolve()
        return self.home.parent / ("%s-bots" % self.home.name)  # ~/.alfred -> ~/.alfred-bots

    @property
    def shared_dir(self) -> Path:
        """The memory every agent reads: next to the home and the bots' directories, inside neither.

        The blacklist denies the home and the bots' directories to every bot, and in Claude Code a deny
        rule wins over an allow rule, so a shared folder inside either could never be opened again.
        """
        value = self.get("ALFRED_SHARED_HOME")
        if value:
            return Path(value).expanduser().resolve()
        return self.home.parent / ("%s-shared" % self.home.name)  # ~/.alfred -> ~/.alfred-shared

    @property
    def shared_memory_dir(self) -> Path:
        return self.shared_dir / "memory"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "scheduler.sqlite3"

    @property
    def sessions_path(self) -> Path:
        if self.bot_id:
            return self.bots_state_dir / str(self.bot_id) / "sessions.json"
        return self.state_dir / "sessions.json"

    @property
    def skills_dir(self) -> Path:
        return self.workspace / ".claude" / "skills"

    @property
    def extensions_dir(self) -> Path:
        """What the agent added to itself, one Claude Code plugin per folder. Each bot has its own."""
        return self.workspace / "extensions"

    @property
    def disabled_extensions_dir(self) -> Path:
        return self.workspace / "extensions-disabled"

    @property
    def memory_dir(self) -> Path:
        return self.workspace / "memory"

    @property
    def memory_index(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    @property
    def inbox_dir(self) -> Path:
        return self.workspace / "inbox"

    # -- access control -----------------------------------------------------

    def allowed_users(self) -> Set[str]:
        """Lower-cased usernames and numeric user ids allowed to talk to Consiglieri."""
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
