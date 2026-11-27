"""Configuration: environment variables, with `.env` files as a fallback.

Lookup order for every setting: process environment, then `$CONSIGLIERI_HOME/.env`,
then the `.env` next to this repository. The allow-list is re-read on every
check so access can be changed without restarting the bot.

Before the rename the settings were `ALFRED_*` (rename-compat). Until an install is migrated (`consiglieri
migrate`), every `CONSIGLIERI_X` lookup also takes the old name from the same place when the new one is not set there, and the
new name wins when both are (see `env_get` and `Config.get`; docs/RENAME_PLAN.md, phase 3).
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = Path.home() / ".consiglieri"
LEGACY_HOME = Path.home() / ".alfred"  # rename-compat: the home before the rename, until `consiglieri migrate`
LEGACY_WHITELIST_PREFIX = "whitelisted@"
PREFIX = "CONSIGLIERI_"
COMMAND = "consiglieri"  # the command a bot or the user types, and the home's launcher in bin/
# rename-compat: the old command. Its launcher stays in the home's bin/ until the install is migrated, so
# every rule that names the command (allow, deny, sandbox exclusions) is written for each name in COMMAND_NAMES.
LEGACY_COMMAND = "alfred"  # rename-compat
COMMAND_NAMES = (COMMAND, LEGACY_COMMAND)  # rename-compat
LEGACY_PREFIX = "ALFRED_"  # rename-compat: the settings' names before the rename

log = logging.getLogger("consiglieri.config")
_warned: Set[str] = set()


def default_home() -> Path:
    """The home when no setting names one: ~/.consiglieri, or an install's old ~/.consiglieri while only that exists.
    Always resolved, like a home that is set, so the bots' and the shared folders derived from it agree."""
    if not DEFAULT_HOME.exists() and LEGACY_HOME.exists():  # rename-compat
        return LEGACY_HOME.resolve()  # rename-compat
    return DEFAULT_HOME.resolve()


def legacy_name(name: str) -> str:
    """The old name of a setting (rename-compat: `CONSIGLIERI_X` -> `ALFRED_X`), or "" for one without the prefix."""
    return LEGACY_PREFIX + name[len(PREFIX):] if name.startswith(PREFIX) else ""  # rename-compat


def _pick(values: Dict[str, str], name: str) -> str:
    """`name` from one source, or its old name from the same source when `name` is not set there."""
    value = str(values.get(name) or "").strip()
    old = legacy_name(name)
    if not old:
        return value
    old_value = str(values.get(old) or "").strip()
    if value and old_value and value != old_value and name not in _warned:
        _warned.add(name)
        log.warning("both %s and %s are set; using %s", name, old, name)  # rename-compat
    return value or old_value


def env_get(name: str, default: str = "", environ: Optional[Dict[str, str]] = None) -> str:
    """A variable of the process environment (the agent's, for the command), with its old name as a fallback."""
    return _pick(dict(os.environ if environ is None else environ), name) or default


def in_bot_turn(environ: Optional[Dict[str, str]] = None) -> bool:
    """Whether this process runs in a bot's turn: a bot id or key under either name. Strict on purpose: an
    empty new name never hides an old one, because the commands only the user may run check this."""
    environ = dict(os.environ if environ is None else environ)
    names = ("CONSIGLIERI_BOT_ID", "CONSIGLIERI_BOT_KEY")
    names += tuple(legacy_name(name) for name in names)  # rename-compat
    return any(str(environ.get(name) or "").strip("0 ") for name in names)


def with_legacy_names(env: Dict[str, str]) -> Dict[str, str]:
    """The agent's environment with each `CONSIGLIERI_X` also under its old name (rename-compat), for scripts written before the
    rename (an extension's, a bot's own). The command and Consiglieri read only the new names first."""
    legacy = {legacy_name(key): value for key, value in env.items() if legacy_name(key)}  # rename-compat
    return {**env, **legacy}  # an old name inherited from the service's own environment never wins


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
    # Set for one of the bots (see `consiglieri/bots.py`); 0 is the control panel, which runs no agent.
    bot_id: int = 0
    # Where this bot's agent runs and keeps its memory and inbox. None for the panel: then it is the home.
    workspace_dir: Optional[Path] = None
    # Per-bot settings, as the same names as the environment (CONSIGLIERI_CLAUDE_MODEL, ...). Read on every lookup,
    # so a change applies from the next message, just like an edit to .env. An unset one falls back to .env.
    overrides: Optional[Callable[[], Dict[str, str]]] = None

    @classmethod
    def load(cls, environ: Optional[Dict[str, str]] = None) -> "Config":
        environ = dict(os.environ if environ is None else environ)
        # CONSIGLIERI_ENV_FILE points at a different settings file than the one next to the code.
        repo_env = Path(_pick(environ, "CONSIGLIERI_ENV_FILE") or REPO_ROOT / ".env").expanduser()
        home_text = _pick(environ, "CONSIGLIERI_HOME") or _pick(_parse_env_lines(_read_lines(repo_env)), "CONSIGLIERI_HOME")
        home = Path(home_text).expanduser().resolve() if home_text else default_home()
        return cls(home=home, env_files=[home / ".env", repo_env], environ=environ)

    # -- raw lookup ---------------------------------------------------------

    def _file_lines(self) -> List[str]:
        lines: List[str] = []
        for path in self.env_files:
            lines.extend(_read_lines(path))
        return lines

    def get(self, name: str, default: str = "") -> str:
        # Each place in turn, the new name before the old one in that same place: a bot's own setting under
        # the old name still beats the .env, and a new name in the environment beats an old one in a file.
        if self.overrides is not None:
            value = _pick(self.overrides(), name)
            if value:
                return value
        value = _pick(self.environ, name)
        if value:
            return value
        for path in self.env_files:
            value = _pick(_parse_env_lines(_read_lines(path)), name)
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
        return self.get("CONSIGLIERI_BACKEND", "claude").lower()

    @property
    def claude_bin(self) -> str:
        return self.get("CONSIGLIERI_CLAUDE_BIN", "claude")

    @property
    def claude_model(self) -> str:
        return self.get("CONSIGLIERI_CLAUDE_MODEL")

    @property
    def claude_effort(self) -> str:
        """How hard Claude thinks: low, medium, high, xhigh or max. Unset means Claude Code's own setting."""
        return self.get("CONSIGLIERI_CLAUDE_EFFORT").lower()

    @property
    def claude_permission_mode(self) -> str:
        return self.get("CONSIGLIERI_CLAUDE_PERMISSION_MODE", "bypassPermissions")

    @property
    def claude_extra_args(self) -> List[str]:
        """Extra flags for every `claude` call, for example `--chrome` to use Claude in Chrome."""
        return shlex.split(self.get("CONSIGLIERI_CLAUDE_EXTRA_ARGS"))

    @property
    def browser(self) -> str:
        """Which browser the agent drives: `orca` (default), `chrome` (Claude in Chrome), `bot` (each bot its own
        Chromium, driven with `consiglieri browser`, where KeePassXC fills in its credentials), or `none`."""
        value = self.get("CONSIGLIERI_BROWSER", "orca").lower()
        return value if value in ("orca", "chrome", "bot", "none") else "orca"

    @property
    def chrome_profile(self) -> str:
        """The Chrome profile folder kept for Consiglieri (for example `Consiglieri`), so `consiglieri chrome` can open it."""
        return self.get("CONSIGLIERI_CHROME_PROFILE")

    @property
    def chrome_app(self) -> str:
        """The browser `consiglieri chrome` opens: an app name on macOS, a command elsewhere."""
        return self.get("CONSIGLIERI_CHROME_APP", "Google Chrome" if sys.platform == "darwin" else "google-chrome")

    @property
    def orca_bin(self) -> str:
        return self.get("CONSIGLIERI_ORCA_BIN", "orca")

    @property
    def codex_bin(self) -> str:
        return self.get("CONSIGLIERI_CODEX_BIN", "codex")

    @property
    def codex_model(self) -> str:
        return self.get("CONSIGLIERI_CODEX_MODEL")

    @property
    def codex_effort(self) -> str:
        """Codex's `model_reasoning_effort` (for example low, medium, high). Unset means Codex's own setting."""
        return self.get("CONSIGLIERI_CODEX_EFFORT").lower()

    @property
    def codex_sandbox(self) -> str:
        return self.get("CONSIGLIERI_CODEX_SANDBOX", "danger-full-access")

    @property
    def live(self) -> bool:
        """Keep one agent process open per chat instead of starting one for every message."""
        return self.get_bool("CONSIGLIERI_LIVE", True)

    @property
    def timeout_seconds(self) -> float:
        """How long a turn may go without producing anything before it is stopped.

        For a backend that starts one process per message, the limit for the whole turn.
        """
        return float(self.get("CONSIGLIERI_TIMEOUT", "900"))

    @property
    def autosave(self) -> bool:
        """Commit the home directory to its own git repo after each turn."""
        return self.get_bool("CONSIGLIERI_AUTOSAVE", True)

    @property
    def show_progress(self) -> bool:
        return self.get_bool("CONSIGLIERI_PROGRESS", True)

    def _bot_value(self, name: str) -> str:
        """A setting only a created bot has. It comes from the bot's own settings, never from Consiglieri's .env."""
        if not self.bot_id or self.overrides is None:
            return ""
        return _pick(self.overrides(), name)

    @property
    def workdir(self) -> Optional[Path]:
        """The directory a bot does its job in, besides its own. None: it works in its own directory."""
        value = self._bot_value("CONSIGLIERI_WORKDIR")
        return Path(value).expanduser() if value else None

    @property
    def add_dirs(self) -> List[Path]:
        """Directories handed to Claude Code with --add-dir: a bot's working directory and extra folders."""
        value = self._bot_value("CONSIGLIERI_ADD_DIRS")
        return [Path(part) for part in value.split(os.pathsep) if part]

    @property
    def claude_settings_file(self) -> Optional[Path]:
        """A bot's generated Claude Code settings (its permissions), passed with --settings."""
        value = self._bot_value("CONSIGLIERI_CLAUDE_SETTINGS")
        return Path(value) if value else None

    # -- paths --------------------------------------------------------------

    @property
    def workspace(self) -> Path:
        """A bot's own directory: where its agent runs, remembers and receives files. The home for the panel,
        which runs no agent there."""
        return self.workspace_dir or self.home

    @property
    def bots_state_dir(self) -> Path:
        """Settings, sessions and tokens of the bots Consiglieri created, one folder each. Never committed."""
        return self.state_dir / "bots"

    @property
    def bots_home(self) -> Path:
        """Where the bots' own directories live: next to the home, never inside it.

        Claude Code loads every CLAUDE.md from its working directory up to the root, so a bot running
        inside the home would also load whatever manual is there (an old home keeps the old main agent's).
        """
        value = self.get("CONSIGLIERI_BOTS_HOME")
        if value:
            return Path(value).expanduser().resolve()
        return self.home.parent / ("%s-bots" % self.home.name)  # ~/.consiglieri -> ~/.consiglieri-bots

    @property
    def shared_dir(self) -> Path:
        """The memory every agent reads: next to the home and the bots' directories, inside neither.

        The blacklist denies the home and the bots' directories to every bot, and in Claude Code a deny
        rule wins over an allow rule, so a shared folder inside either could never be opened again.
        """
        value = self.get("CONSIGLIERI_SHARED_HOME")
        if value:
            return Path(value).expanduser().resolve()
        return self.home.parent / ("%s-shared" % self.home.name)  # ~/.consiglieri -> ~/.consiglieri-shared

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
        """Lower-cased usernames and numeric user ids allowed to use the panel; each creates and manages only their own bots."""
        allowed = _legacy_whitelist(self._file_lines())
        for entry in self.get("CONSIGLIERI_ALLOWED_USERS").replace(";", ",").split(","):
            entry = entry.strip().lstrip("@").lower()
            if entry:
                allowed.add(entry)
        return allowed

    def is_allowed(self, user_id: Optional[int], username: Optional[str]) -> bool:
        allowed = self.allowed_users()
        if user_id is not None and str(user_id) in allowed:
            return True
        return bool(username) and username.lstrip("@").lower() in allowed

    def may_grant_credentials(self, user_id: Optional[int]) -> bool:
        """Whether this panel user may give the machine's credentials to their bots from Telegram. The store is
        shared by the whole machine, so with several panel users only those in CONSIGLIERI_CREDENTIALS_GRANTERS
        (numeric Telegram ids) may; with a single one, that user may unless the setting says otherwise."""
        listed = self.get("CONSIGLIERI_CREDENTIALS_GRANTERS").strip()
        if listed:
            granters = {entry.strip() for entry in listed.replace(";", ",").split(",") if entry.strip()}
            return user_id is not None and str(user_id) in granters
        return len(self.allowed_users()) <= 1
