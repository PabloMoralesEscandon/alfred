"""Consiglieri's home, the control panel's state directory, and what every bot's directory shares with it.

The home is deliberately outside this repository and runs no agent: it holds the panel's state (the bots'
registry and tokens, the scheduler, the audit log, the usage history, the base personality) under
`state/`, and `bin/consiglieri`, the launcher every bot finds on its PATH. Each bot has its own directory
next to it (see bots.py); the skill and memory helpers here work on whichever directory a bot's
configuration names.

Homes from before the panel (when the main bot ran an agent here) still hold that agent's CLAUDE.md,
SOUL.md, memory/ and extensions/. Nothing reads or rewrites them any more, and every bot is denied the
whole home; docs/PANEL_ONLY.md says what to do with them.
"""

from __future__ import annotations

import hashlib
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .config import COMMAND_NAMES, REPO_ROOT, Config

TEMPLATES = Path(__file__).resolve().parent / "templates"


@dataclass
class Skill:
    name: str  # as Claude Code shows it: `consiglieri:browser`, `<extension>:<skill>`, or a bare local name
    description: str
    path: Path
    source: str = "local"  # base, extension, or local (in .claude/skills, outside any extension)


def parse_frontmatter(text: str) -> Dict[str, str]:
    """Read the flat `key: value` block between the leading `---` lines of a SKILL.md."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: Dict[str, str] = {}
    key: Optional[str] = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t") and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()  # folded continuation
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key = name.strip()
        value = value.strip()
        if value in (">", "|", ">-", "|-"):
            value = ""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key] = value
    return fields


def _skills_in(directory: Path, prefix: str, source: str) -> List[Skill]:
    skills: List[Skill] = []
    if not directory.is_dir():
        return skills
    for skill_file in sorted(directory.glob("*/SKILL.md")):
        try:
            fields = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = skill_file.parent.name if prefix else (fields.get("name") or skill_file.parent.name)
        skills.append(
            Skill(name=prefix + name, description=fields.get("description", ""), path=skill_file, source=source)
        )
    return skills


def list_skills(config: Config) -> List[Skill]:
    """Every skill the agent has: the base plugin's, each enabled extension's, then loose local ones."""
    from . import extensions

    skills = _skills_in(extensions.BASE_PLUGIN / "skills", extensions.BASE_NAME + ":", "base")
    for extension in extensions.list_extensions(config, include_disabled=False):
        skills += _skills_in(extension.path / "skills", extension.name + ":", "extension")
    return skills + _skills_in(config.skills_dir, "", "local")


def skills_index(config: Config) -> str:
    """A prompt block for backends that do not discover skills on their own."""
    skills = list_skills(config)
    if not skills:
        return ""
    lines = ["<skills>", "Read a skill's file before doing the kind of task it describes."]
    for skill in skills:
        try:
            where = str(skill.path.relative_to(config.workspace))
        except ValueError:
            where = str(skill.path)
        lines.append("- %s (%s): %s" % (skill.name, where, skill.description))
    lines.append("</skills>")
    return "\n".join(lines)


@dataclass
class Memory:
    key: str  # path inside memory/ without the extension, for example "people/sam"
    description: str
    path: Path


def _memory_files(config: Config) -> List[Path]:
    if not config.memory_dir.is_dir():
        return []
    return sorted(path for path in config.memory_dir.rglob("*.md") if path != config.memory_index)


def _memory_key(config: Config, path: Path) -> str:
    return path.relative_to(config.memory_dir).with_suffix("").as_posix()


def list_memories(config: Config) -> List[Memory]:
    memories: List[Memory] = []
    for path in _memory_files(config):
        try:
            fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            fields = {}
        memories.append(Memory(key=_memory_key(config, path), description=fields.get("description", ""), path=path))
    return memories


def memory_snapshot(config: Config) -> Dict[str, str]:
    """Fingerprint of every memory file, to tell what a turn saved, changed or forgot."""
    snapshot: Dict[str, str] = {}
    for path in _memory_files(config):
        try:
            snapshot[_memory_key(config, path)] = hashlib.sha1(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return snapshot


def memory_changes(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Human-readable notes such as "saved people/sam" for the difference between two snapshots."""
    notes = ["saved %s" % key for key in sorted(after) if key not in before]
    notes += ["updated %s" % key for key in sorted(after) if key in before and before[key] != after[key]]
    notes += ["forgot %s" % key for key in sorted(before) if key not in after]
    return notes


def _write_if_changed(path: Path, content: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    path.write_text(content, encoding="utf-8")


def _write_launcher(config: Config) -> None:
    """`bin/consiglieri` runs this checkout's CLI with the interpreter that started the panel. Every bot has it on
    PATH. While the old name lasts, `bin/consiglieri` is the same script: the bots' rules are written for both names."""
    script = (
        "#!/bin/sh\n"
        "# Managed by Consiglieri. Rewritten on every start.\n"
        "export CONSIGLIERI_HOME=%s\n"
        'PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH} exec %s -m consiglieri "$@"\n'
        % (shlex.quote(str(config.home)), shlex.quote(str(REPO_ROOT)), shlex.quote(sys.executable))
    )
    for name in COMMAND_NAMES:
        launcher = config.home / "bin" / name
        launcher.parent.mkdir(parents=True, exist_ok=True)
        _write_if_changed(launcher, script)
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=Consiglieri", "-c", "user.email=consiglieri@localhost", *args],
        cwd=str(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def autosave(config: Config, message: str) -> bool:
    """Commit whatever changed in a bot's own directory. True if a commit was made."""
    folder = config.workspace
    if not config.bot_id or not config.autosave or not (folder / ".git").exists():
        return False  # never the home: the panel's state is not versioned, and an old home's history stays as it is
    _git(folder, "add", "-A")
    if _git(folder, "diff", "--cached", "--quiet").returncode == 0:
        return False
    return _git(folder, "commit", "-q", "-m", message).returncode == 0


def prepare(config: Config) -> Path:
    """Create or refresh the panel's state directory. Safe to run on every start.

    It makes `state/`, rewrites the launcher and creates the shared memory if it is missing. It never
    writes an agent's manual, personality or memory here: the panel has no agent.
    """
    for directory in (config.home, config.state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    _write_launcher(config)
    from . import shared_memory  # late: shared_memory imports extensions, which imports this module

    try:
        shared_memory.ensure(config)
    except OSError:
        pass  # reported by shared_memory itself when a bot first needs it
    return config.home
