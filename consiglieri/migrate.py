"""`consiglieri migrate`: move an install from before the rename to the new names (docs/RENAME_PLAN.md, phase 4).

This whole module is about the old names on purpose (rename-compat). It never runs by itself, and with
`--dry-run` it only says what it would do. With the service stopped and removed, it:

1. moves the home, the bots' directories and the shared memory (`~/.alfred`, `~/.alfred-bots`,
   `~/.alfred-shared`) to their new names (`~/.consiglieri`, ...);
2. moves Claude Code's folders for them (`~/.claude/projects/<path with - for every other character>`), so
   every bot resumes its conversations and keeps Claude Code's own memory;
3. rewrites the old absolute paths in the panel's state (bots' settings, KeePassXC's settings, ...) and in
   the `.env` files, and renames their `ALFRED_*` settings to `CONSIGLIERI_*`;
4. rewrites the launcher in the new home's `bin/`, removes the old command's one, and lists what the bots
   wrote for themselves (extensions, skills) that still calls the old command, without changing it.

It refuses, before touching anything, while a service (old or new name) is installed, while Consiglieri,
its KeePassXC or a bot's browser runs, when a destination already exists, or in a bot's turn. It leaves no
link at the old paths: a link would be a second path to every folder the bots are denied.
Afterwards: `consiglieri service install` (which writes `com.consiglieri.bot`), then `consiglieri browser-setup`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import LEGACY_PREFIX, PREFIX, Config, in_bot_turn

OLD_NAME = "alfred"  # rename-compat
NEW_NAME = "consiglieri"
TEXT_SUFFIXES = (".json", ".ini")  # the state files that can hold an absolute path
MAX_TEXT_BYTES = 1 << 20
SKIPPED_STATE = ("browsers",)  # Chromium profiles: big, and their paths are rewritten when a browser starts
OLD_COMMAND_USE = re.compile(r"(^|[\s/`\"'(])%s( [a-z]|:[a-z])" % OLD_NAME, re.M)  # `alfred send`, `alfred:browser`
RUNNING = re.compile(r"(-m (alfred|consiglieri)|(^|/)(alfred|consiglieri)) run\b")  # rename-compat


class MigrationError(Exception):
    pass


@dataclass
class Step:
    """One thing the migration does. `run` does it; the description is what --dry-run shows."""

    description: str
    run: Callable[[], None] = field(repr=False)


def project_key(path: Path) -> str:
    """Claude Code's folder name for a working directory (bots.claude_project_dir)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def replace_paths(text: str, pairs: List[Tuple[Path, Path]]) -> str:
    """Every old absolute path in `text` (as a whole path component: ~/.alfred never matches ~/.alfred-test)."""
    for old, new in sorted(pairs, key=lambda pair: -len(str(pair[0]))):
        text = re.sub(re.escape(str(old)) + r"(?![A-Za-z0-9_.-])", lambda _: str(new), text)
    return text


def rename_settings(text: str) -> Tuple[str, List[str]]:
    """`ALFRED_X=` lines of a .env file as `CONSIGLIERI_X=`, values untouched. Returns the text and the names.
    A name already set under its new name is left alone (the new one wins anyway)."""
    present = set(re.findall(r"^\s*(?:export\s+)?(%s[A-Z0-9_]+)\s*=" % PREFIX, text, re.M))
    renamed: List[str] = []

    def rename(match: "re.Match[str]") -> str:
        new = PREFIX + match.group(2)[len(LEGACY_PREFIX):]
        if new in present:
            return match.group(0)
        renamed.append(match.group(2))
        return match.group(1) + new

    text = re.sub(r"^(\s*(?:export\s+)?)(%s[A-Z0-9_]+)(?=\s*=)" % LEGACY_PREFIX, rename, text, flags=re.M)
    return text, renamed


def _rewrite(path: Path, transform: Callable[[str], str]) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    changed = transform(text)
    if changed == text:
        return False
    mode = path.stat().st_mode
    temporary = path.with_name(path.name + ".migrating")
    temporary.write_text(changed, encoding="utf-8")
    os.chmod(temporary, mode & 0o7777)
    os.replace(temporary, path)
    return True


def _processes() -> List[str]:
    result = subprocess.run(["ps", "-x", "-o", "command="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, check=False)
    return result.stdout.splitlines()


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Migration:
    """Everything is worked out first (`steps`), so that --dry-run and the real run do the same thing."""

    def __init__(self, config: Config, source: Optional[Path] = None, target: Optional[Path] = None,
                 user_home: Optional[Path] = None, claude_dir: Optional[Path] = None,
                 processes: Optional[Callable[[], List[str]]] = None) -> None:
        self.config = config
        self.user_home = user_home or Path.home()
        source = Path(source or self.user_home / (".%s" % OLD_NAME)).expanduser().absolute()
        target = Path(target or self.user_home / (".%s" % NEW_NAME)).expanduser().absolute()
        # Resolved like the home is (config.Config.load), but never through a link at the old path itself.
        self.source = source.parent.resolve() / source.name
        self.target = target.parent.resolve() / target.name
        if claude_dir is None:
            from .bots import claude_config_dir

            claude_dir = claude_config_dir(config)
        self.projects = claude_dir / "projects"
        self.processes = processes or _processes

    # -- what moves --

    def pairs(self) -> List[Tuple[Path, Path]]:
        """The home and the folders named after it, as (old, new), the ones that exist."""
        found = []
        for suffix in ("", "-bots", "-shared"):
            old = self.source.parent / (self.source.name + suffix)
            new = self.target.parent / (self.target.name + suffix)
            if old.exists() or old.is_symlink():
                found.append((old, new))
        return found

    def project_moves(self, pairs: List[Tuple[Path, Path]]) -> List[Tuple[Path, Path]]:
        """Claude Code's folders for the home, the bots' directories (each bot's own folder) and the shared one."""
        working: List[Tuple[Path, Path]] = []
        for old, new in pairs:
            working.append((old, new))
            if old.is_dir() and not old.is_symlink():
                working += [(old / child.name, new / child.name) for child in sorted(old.iterdir()) if child.is_dir()]
        moves = []
        for old, new in working:
            folder = self.projects / project_key(old)
            if folder.is_dir():
                moves.append((folder, self.projects / project_key(new)))
        return moves

    def text_files(self) -> List[Path]:
        """Under the home's state/ (as it is now): the small settings files that may name a path."""
        state = self.source / "state"
        found = []
        if state.is_dir():
            for path in sorted(state.rglob("*")):
                relative = path.relative_to(state)
                if relative.parts and relative.parts[0] in SKIPPED_STATE:
                    continue
                if path.is_file() and not path.is_symlink() and path.suffix in TEXT_SUFFIXES \
                        and path.stat().st_size <= MAX_TEXT_BYTES:
                    found.append(path)
        return found

    def env_files(self) -> List[Path]:
        """The home's .env and the one next to the code (or CONSIGLIERI_ENV_FILE's), each once."""
        found: List[Path] = []
        for path in [self.source / ".env"] + [path for path in self.config.env_files if path != self.config.home / ".env"]:
            if path.is_file() and path not in found:
                found.append(path)
        return found

    # -- checks --

    def problems(self, pairs: List[Tuple[Path, Path]], projects: List[Tuple[Path, Path]]) -> List[str]:
        from . import service
        from .keepass import KeePass

        found: List[str] = []
        if in_bot_turn(dict(self.config.environ)) or in_bot_turn():
            return ["`consiglieri migrate` is for the user, on the machine; a bot never runs it"]
        if not self.source.is_dir():
            return ["there is nothing to migrate: %s does not exist" % self.source]
        if self.source == self.target:
            return ["the old and the new home are the same folder"]
        if self.source.is_symlink():
            found.append("%s is a link; move the folder it points to instead" % self.source)
        for old, new in pairs:
            if new.exists() or new.is_symlink():
                found.append("%s already exists; move it away first (nothing has been moved)" % new)
        for _, new in projects:
            if new.exists():
                found.append("Claude Code's folder %s already exists" % new)
        units = [self.user_home / "Library" / "LaunchAgents" / ("%s.plist" % label)
                 for label in (service.LEGACY_LABEL, service.LABEL)]
        units += [self.user_home / ".config" / "systemd" / "user" / unit
                  for unit in (service.LEGACY_SYSTEMD_UNIT, service.SYSTEMD_UNIT)]
        for unit in units:
            if unit.exists():
                found.append("the service %s is installed; stop and remove it first: consiglieri service uninstall"
                             % unit.name)
        running = [line.strip() for line in self.processes() if RUNNING.search(line)]
        if running:
            found.append("Consiglieri is still running (%s); stop it first" % running[0][:120])
        old_config = Config(home=self.source, env_files=[], environ=dict(self.config.environ))
        if KeePass(old_config).running():
            found.append("its KeePassXC is running; stop it first: consiglieri creds stop")
        for run_file in sorted((self.source / "state" / "browsers").glob("*/run.json")):
            try:
                pid = int(json.loads(run_file.read_text(encoding="utf-8")).get("pid") or 0)
            except (OSError, ValueError, AttributeError):
                continue
            if _alive(pid):
                found.append("a bot's browser is open (%s); close it first: consiglieri browser stop --bot %s"
                             % (run_file.parent.name, run_file.parent.name))
        return found

    # -- the plan --

    def steps(self) -> List[Step]:
        pairs = self.pairs()
        projects = self.project_moves(pairs)
        problems = self.problems(pairs, projects)
        if problems:
            raise MigrationError("not migrating:\n- " + "\n- ".join(problems))
        # Worked out before anything moves: the paths below are where these files are now.
        text_files = self.text_files()
        env_files = self.env_files()
        steps: List[Step] = []

        def mover(old: Path, new: Path) -> Callable[[], None]:
            def move() -> None:
                new.parent.mkdir(parents=True, exist_ok=True)
                os.rename(str(old), str(new))
            return move

        for old, new in pairs:
            steps.append(Step("move %s -> %s" % (old, new), mover(old, new)))
        for old, new in projects:
            steps.append(Step("move Claude Code's %s -> %s" % (old.name, new.name), mover(old, new)))

        def moved(path: Path) -> Path:
            for old, new in pairs:
                try:
                    return new / path.relative_to(old)
                except ValueError:
                    continue
            return path

        def paths_in(path: Path) -> Callable[[], None]:
            def rewrite() -> None:
                _rewrite(moved(path), lambda text: _shell_value(replace_paths(text, pairs)))
            return rewrite

        for path in text_files:
            text = path.read_text(encoding="utf-8", errors="replace")
            if replace_paths(text, pairs) != text or _shell_value(text) != text:
                steps.append(Step("rewrite the old paths in %s" % moved(path), paths_in(path)))
        for path in env_files:
            text = path.read_text(encoding="utf-8", errors="replace")
            _, names = rename_settings(text)
            paths_changed = replace_paths(text, pairs) != text
            if names or paths_changed:
                what = []
                if names:
                    what.append("rename %s to %s*" % (", ".join(names), PREFIX))
                if paths_changed:
                    what.append("rewrite the old paths")
                steps.append(Step("%s in %s (values kept)" % (" and ".join(what), moved(path)), _env_rewriter(moved(path), pairs)))

        def launchers() -> None:
            from . import home

            old_launcher = self.target / "bin" / OLD_NAME
            if old_launcher.exists() or old_launcher.is_symlink():
                old_launcher.unlink()  # the old command goes with the old home; a migrated home never gets it back
            home._write_launcher(Config(home=self.target, env_files=[], environ=dict(self.config.environ)))

        steps.append(Step("rewrite %s and remove the old command's launcher" % (self.target / "bin" / NEW_NAME),
                          launchers))
        for path in self.old_command_users():
            steps.append(Step("note: %s still calls the old command; change it to consiglieri (not changed here)"
                              % moved(path), lambda: None))
        return steps

    def old_command_users(self) -> List[Path]:
        """What the bots wrote for themselves (extensions, their own skills) that runs the old command: after the
        migration that command is gone, so these are listed for the user. Nothing here is rewritten."""
        bots = self.source.parent / (self.source.name + "-bots")
        found: List[Path] = []
        for bot in sorted(bots.iterdir()) if bots.is_dir() else []:
            for part in ("extensions", ".claude/skills"):
                folder = bot / part
                for path in sorted(folder.rglob("*")) if folder.is_dir() else []:
                    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_TEXT_BYTES:
                        continue
                    try:
                        text = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if OLD_COMMAND_USE.search(text):
                        found.append(path)
        return found

    def run(self, dry_run: bool = False) -> List[str]:
        steps = self.steps()
        done = []
        for step in steps:
            if not dry_run:
                step.run()
            done.append(step.description)
        return done


def _shell_value(text: str) -> str:
    """A bot's stored shell permission under its old name (bots.BotRecord.from_json reads it either way)."""
    return re.sub(r'("shell"\s*:\s*)"%s"' % OLD_NAME, r'\1"%s"' % NEW_NAME, text)


def _env_rewriter(path: Path, pairs: List[Tuple[Path, Path]]) -> Callable[[], None]:
    def rewrite() -> None:
        _rewrite(path, lambda text: rename_settings(replace_paths(text, pairs))[0])
    return rewrite


NEXT_STEPS = (
    "consiglieri service install      (installs com.consiglieri.bot with the new home, and starts it)",
    "consiglieri service status",
    "consiglieri browser-setup        (links the new home to its Orca workspace again)",
)


