"""Alfred's home directory: the agent's working directory, memory, and self-written skills.

The home is deliberately outside this repository. The code here is the
runtime; the home is the agent's own state, versioned in its own git repo so
anything it teaches itself can be inspected and rolled back.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .config import REPO_ROOT, Config

TEMPLATES = Path(__file__).resolve().parent / "templates"
MANAGED_FILES = ("CLAUDE.md", "AGENTS.md")
LEGACY_SKILL_NAME = "legacy-notes"
INDEX_NAME = "MEMORY.md"
CARRIED_OVER = "general"


# Skills that earlier versions copied into the home. They now load from the base plugin.
RETIRED_SEEDS = ("browser", "create-skill", "extensions", "orca-agents", "reminders", "web-logins")


@dataclass
class Skill:
    name: str  # as Claude Code shows it: `alfred:browser`, `<extension>:<skill>`, or a bare local name
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
            where = str(skill.path.relative_to(config.home))
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


def _carry_over_memory(config: Config, facts: List[str], source: str) -> None:
    """Put facts from an older single-file memory into the folder layout as one file plus its index line."""
    description = "Facts carried over from %s. Split them into topic files when you next touch them." % source
    target = config.memory_dir / ("%s.md" % CARRIED_OVER)
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (CARRIED_OVER, description, "\n".join(facts)),
        encoding="utf-8",
    )
    index = config.memory_index.read_text(encoding="utf-8") if config.memory_index.exists() else "# Memory index\n\n"
    config.memory_index.write_text(
        index.rstrip("\n") + "\n\n- [%s](%s.md): %s\n" % (CARRIED_OVER, CARRIED_OVER, description), encoding="utf-8"
    )


def _migrate_single_file_memory(config: Config) -> bool:
    """Homes created before the memory folder kept everything in one MEMORY.md at the top level."""
    old = config.home / INDEX_NAME
    if not old.is_file() or config.memory_index.exists():
        return False
    facts = [line.rstrip() for line in old.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if facts:
        _carry_over_memory(config, facts, "the earlier single-file memory")
    old.unlink()
    return bool(facts)


def _write_if_changed(path: Path, content: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    path.write_text(content, encoding="utf-8")


def _write_launcher(config: Config) -> None:
    """`bin/alfred` runs this checkout's CLI with the interpreter that started the bot."""
    launcher = config.home / "bin" / "alfred"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        "# Managed by Alfred. Rewritten on every start.\n"
        "export ALFRED_HOME=%s\n"
        'PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH} exec %s -m alfred "$@"\n'
        % (shlex.quote(str(config.home)), shlex.quote(str(REPO_ROOT)), shlex.quote(sys.executable))
    )
    _write_if_changed(launcher, script)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _import_legacy(config: Config, legacy_dir: Path) -> List[str]:
    """Carry over SOUL.md, SKILLS.md and remembered facts from the pre-refactor layout, once."""
    imported: List[str] = []

    soul = legacy_dir / "SOUL.md"
    if soul.is_file() and not (config.home / "SOUL.md").exists():
        shutil.copyfile(soul, config.home / "SOUL.md")
        imported.append("SOUL.md")

    skills = legacy_dir / "SKILLS.md"
    target = config.skills_dir / LEGACY_SKILL_NAME
    if skills.is_file() and not target.exists():
        target.mkdir(parents=True)
        body = skills.read_text(encoding="utf-8").strip()
        (target / "SKILL.md").write_text(
            "---\n"
            "name: %s\n"
            "description: Tool and workflow notes carried over from the old SKILLS.md. Check it when the user asks "
            "for something you have no dedicated skill for, then split what you use into proper skills.\n"
            "---\n\n%s\n" % (LEGACY_SKILL_NAME, body),
            encoding="utf-8",
        )
        imported.append("SKILLS.md")

    memory_files = sorted((legacy_dir / "memory" / "chats").glob("*/memory.md"))
    if memory_files and not config.memory_index.exists() and not (config.home / INDEX_NAME).exists():
        facts: List[str] = []
        for memory_file in memory_files:
            for line in memory_file.read_text(encoding="utf-8").splitlines():
                if line.strip() and line.strip() not in facts:
                    facts.append(line.strip())
        if facts:
            _carry_over_memory(config, facts, "the first version of Alfred")
            imported.append("memory")

    return imported


def _git(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=Alfred", "-c", "user.email=alfred@localhost", *args],
        cwd=str(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def autosave(config: Config, message: str) -> bool:
    """Commit whatever changed in the home directory. Returns True if a commit was made."""
    if not config.autosave or not (config.home / ".git").exists():
        return False
    _git(config.home, "add", "-A")
    if _git(config.home, "diff", "--cached", "--quiet").returncode == 0:
        return False
    return _git(config.home, "commit", "-q", "-m", message).returncode == 0


def _retire_seed_copies(config: Config) -> None:
    """Move old copies of the built-in skills out of `.claude/skills`, where they would shadow the base plugin.

    They go to `state/retired-skills/`, and their history stays in git.
    """
    for name in RETIRED_SEEDS:
        copy = config.skills_dir / name
        if not copy.is_dir():
            continue
        target = config.state_dir / "retired-skills" / name
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(copy), str(target))


def bootstrap(config: Config, legacy_dir: Optional[Path] = REPO_ROOT) -> List[str]:
    """Create or refresh the home directory. Safe to run on every start.

    Managed files (the operating manual and the launcher) are always
    rewritten. Everything the user or the agent owns is only created when
    missing. Returns the names of legacy items that were imported.
    """
    home = config.home
    for directory in (home, config.state_dir, config.inbox_dir, config.skills_dir, config.extensions_dir):
        directory.mkdir(parents=True, exist_ok=True)

    imported = _import_legacy(config, legacy_dir) if legacy_dir else []
    _migrate_single_file_memory(config)
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    if not config.memory_index.exists():
        shutil.copyfile(TEMPLATES / INDEX_NAME, config.memory_index)

    for name in MANAGED_FILES:
        _write_if_changed(home / name, (TEMPLATES / name).read_text(encoding="utf-8"))
    _write_launcher(config)

    for name, source in (("SOUL.md", "SOUL.md"), (".gitignore", "gitignore")):
        if not (home / name).exists():
            shutil.copyfile(TEMPLATES / source, home / name)

    _retire_seed_copies(config)

    if config.autosave and shutil.which("git"):
        if not (home / ".git").exists():
            _git(home, "init", "-q")
        autosave(config, "alfred: bootstrap")

    return imported
