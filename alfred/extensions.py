"""Extensions: what Consiglieri adds to itself, kept apart from what ships with it.

Three layers, each in its own place:

- base: `alfred/plugin/`, a Claude Code plugin shipped with this code. Its
  skills are `alfred:<skill>` and its `bin/` is on PATH. Consiglieri never edits it.
- extensions: `~/.alfred/extensions/<name>/`, one Claude Code plugin per
  extension, written by Consiglieri. Skills are `<name>:<skill>`, executables in
  `bin/` are bare commands, and hooks or MCP servers load with the plugin.
- disabled extensions: `~/.alfred/extensions-disabled/<name>/`, kept on disk
  but not loaded.

Every process gets the base plugin and each enabled extension with
`--plugin-dir`. `fingerprint()` changes when an extension's shape changes, and
the agent then reopens its process at the next idle moment so the change loads.
See docs/EXTENSIONS.md for the protocol Consiglieri follows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config

BASE_PLUGIN = Path(__file__).resolve().parent / "plugin"
BASE_NAME = "alfred"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
MAX_NAME = 40
RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
RESERVED_NAMES = {BASE_NAME, "claude", "anthropic", "anthropics", "claude-code", "claude-mods", "base", "core"}
MAX_FILE_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mcpb-cache"}

# Things that look like a credential written into a file. A finding blocks `check`.
SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Anthropic or OpenAI key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("password assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[\"'][^\"'\s$<{]{6,}[\"']")),
)


class ExtensionError(Exception):
    pass


@dataclass
class Extension:
    name: str
    path: Path
    enabled: bool
    version: str = ""
    description: str = ""
    skills: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    hooks: List[str] = field(default_factory=list)
    mcp_servers: List[str] = field(default_factory=list)
    monitors: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)

    def runs_by_itself(self) -> List[str]:
        """The parts that act without being asked: they need the user's approval."""
        parts = ["hook on %s" % event for event in self.hooks]
        parts += ["MCP server %s" % name for name in self.mcp_servers]
        parts += ["monitor %s" % name for name in self.monitors]
        return parts

    def summary(self) -> str:
        bits = []
        if self.skills:
            bits.append("skills %s" % ", ".join(self.skills))
        if self.commands:
            bits.append("commands %s" % ", ".join(self.commands))
        if self.agents:
            bits.append("agents %s" % ", ".join(self.agents))
        bits += self.runs_by_itself()
        return "; ".join(bits) or "nothing in it yet"

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "version": self.version,
            "description": self.description,
            "path": str(self.path),
            "skills": ["%s:%s" % (self.name, skill) for skill in self.skills],
            "commands": self.commands,
            "agents": self.agents,
            "hooks": self.hooks,
            "mcp_servers": self.mcp_servers,
            "monitors": self.monitors,
        }


# -- reading ---------------------------------------------------------------


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_executable(path: Path) -> bool:
    return path.is_file() and bool(path.stat().st_mode & stat.S_IXUSR)


def load(path: Path, enabled: bool) -> Extension:
    manifest = _read_json(path / ".claude-plugin" / "plugin.json")
    extension = Extension(
        name=path.name,
        path=path,
        enabled=enabled,
        version=str(manifest.get("version") or ""),
        description=str(manifest.get("description") or ""),
    )
    skills_dir = path / "skills"
    if skills_dir.is_dir():
        extension.skills = sorted(item.parent.name for item in skills_dir.glob("*/SKILL.md"))
    bin_dir = path / "bin"
    if bin_dir.is_dir():
        extension.commands = sorted(item.name for item in bin_dir.iterdir() if _is_executable(item))
    agents_dir = path / "agents"
    if agents_dir.is_dir():
        extension.agents = sorted(item.stem for item in agents_dir.glob("*.md"))
    hooks: Dict[str, Any] = {}
    hooks.update((_read_json(path / "hooks" / "hooks.json").get("hooks") or {}))
    if isinstance(manifest.get("hooks"), dict):
        hooks.update(manifest["hooks"])
    extension.hooks = sorted(hooks)
    servers: Dict[str, Any] = {}
    servers.update(_read_json(path / ".mcp.json").get("mcpServers") or _read_json(path / ".mcp.json"))
    if isinstance(manifest.get("mcpServers"), dict):
        servers.update(manifest["mcpServers"])
    extension.mcp_servers = sorted(name for name, spec in servers.items() if isinstance(spec, dict))
    monitors = (manifest.get("experimental") or {}).get("monitors") if isinstance(manifest.get("experimental"), dict) else None
    if not isinstance(monitors, list):
        try:
            monitors = json.loads((path / "monitors" / "monitors.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            monitors = []
    extension.monitors = sorted(str(item.get("name")) for item in monitors if isinstance(item, dict)) if isinstance(monitors, list) else []
    return extension


def _children(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(item for item in directory.iterdir() if item.is_dir() and not item.name.startswith("."))


def list_extensions(config: Config, include_disabled: bool = True) -> List[Extension]:
    found = [load(path, True) for path in _children(config.extensions_dir)]
    if include_disabled:
        found += [load(path, False) for path in _children(config.disabled_extensions_dir)]
    return found


def find(config: Config, name: str) -> Extension:
    for extension in list_extensions(config):
        if extension.name == name:
            return extension
    raise ExtensionError("no extension called %r (see `alfred ext list`)" % name)


def held_back(config: Config, extension: Extension) -> List[str]:
    """Why one of a bot's extensions is not loaded: the parts of it that would run by themselves.

    Hooks, monitors and LSP servers run as the user, outside Claude Code's permission rules and its sandbox,
    and a bot writes its own extensions: loading them would let a bot get past every permission it has,
    the blacklist on other agents' memory and tools included. MCP servers load only with the bot's `mcp`
    permission. Consiglieri's main agent (bot 0) loads everything, as before.
    """
    if not config.bot_id:
        return []
    path = extension.path
    manifest = _read_json(path / ".claude-plugin" / "plugin.json")
    experimental = manifest.get("experimental") if isinstance(manifest.get("experimental"), dict) else {}
    reasons = []
    if extension.hooks or manifest.get("hooks") or (path / "hooks").exists():
        reasons.append("hooks")
    if extension.monitors or experimental.get("monitors") or (path / "monitors").exists():
        reasons.append("monitors")
    if manifest.get("lspServers") or (path / ".lsp.json").exists():
        reasons.append("LSP servers")
    has_mcp = extension.mcp_servers or manifest.get("mcpServers") or (path / ".mcp.json").exists()
    if has_mcp and config._bot_value("ALFRED_EXTENSION_MCP") != "on":
        reasons.append("MCP servers (needs the mcp permission)")
    return reasons


def plugin_dirs(config: Config) -> List[Path]:
    """What every agent process loads: the base plugin, then each enabled extension (for a bot, those that
    don't run by themselves; see held_back)."""
    return [BASE_PLUGIN] + [
        extension.path for extension in list_extensions(config, include_disabled=False) if not held_back(config, extension)
    ]


def bin_dirs(config: Config) -> List[Path]:
    """Executable folders to put on PATH, for backends that do not load plugins themselves."""
    return [path / "bin" for path in plugin_dirs(config) if (path / "bin").is_dir()]


def fingerprint(config: Config) -> str:
    """Changes when an extension appears, goes, or changes what it loads (not when a skill's text is edited)."""
    digest = hashlib.sha256()
    for path in plugin_dirs(config)[1:]:
        digest.update(path.name.encode())
        for relative in (".claude-plugin/plugin.json", "hooks/hooks.json", ".mcp.json", "monitors/monitors.json"):
            target = path / relative
            if target.is_file():
                digest.update(relative.encode())
                digest.update(target.read_bytes())
        extension = load(path, True)
        digest.update(json.dumps([extension.skills, extension.commands, extension.agents]).encode())
        for skill in extension.skills:  # the name and description are loaded at start; the body is read when used
            head = (path / "skills" / skill / "SKILL.md").read_text(encoding="utf-8", errors="replace").split("\n---", 1)[0]
            digest.update(head.encode())
    return digest.hexdigest()[:16]


def inventory(config: Config) -> Dict[str, str]:
    """name -> a fingerprint of what it is and holds, for spotting what a turn changed."""
    return {
        extension.name: json.dumps([extension.enabled, extension.version, extension.summary()])
        for extension in list_extensions(config)
    }


def changes(before: Dict[str, str], after: Dict[str, str], config: Config) -> List[str]:
    """Human notes about extensions a turn added, changed or removed. Parts that run by themselves are flagged."""
    notes = []
    for name in sorted(set(before) | set(after)):
        if name not in after:
            notes.append("removed %s" % name)
            continue
        if before.get(name) == after[name]:
            continue
        try:
            extension = find(config, name)
        except ExtensionError:
            continue
        if name not in before:
            verb = "added"
        elif json.loads(before[name])[0] != extension.enabled:
            verb = "enabled" if extension.enabled else "disabled"
        else:
            verb = "updated"
        note = "%s %s%s: %s" % (verb, name, " " + extension.version if extension.version else "", extension.summary())
        if extension.enabled and held_back(config, extension):
            note += " ⚠️ not loaded: a bot's extension can't run by itself (%s)" % ", ".join(held_back(config, extension))
        elif extension.enabled and extension.runs_by_itself():
            note += " ⚠️ runs by itself"
        notes.append(note)
    return notes


# -- changing ----------------------------------------------------------------


def validate_name(config: Config, name: str) -> None:
    if not NAME_PATTERN.match(name) or len(name) > MAX_NAME:
        raise ExtensionError("an extension name is lowercase words joined by hyphens, at most %d characters" % MAX_NAME)
    if name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES):
        raise ExtensionError("%r is reserved" % name)


def scaffold(config: Config, name: str, description: str) -> Extension:
    validate_name(config, name)
    if any(extension.name == name for extension in list_extensions(config)):
        raise ExtensionError("an extension called %r already exists; change it instead" % name)
    if not description.strip():
        raise ExtensionError("give the extension a one-line description")
    path = config.extensions_dir / name
    (path / ".claude-plugin").mkdir(parents=True)
    (path / "skills").mkdir()
    (path / "bin").mkdir()
    manifest = {
        "name": name,
        "version": "0.1.0",
        "description": description.strip(),
        "author": {"name": "Consiglieri"},
    }
    (path / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    today = datetime.now().strftime("%Y-%m-%d")
    (path / "README.md").write_text(
        "# %s\n\n%s\n\n## Needs\n\nNothing outside this folder.\n\n## Changes\n\n- 0.1.0 (%s): created.\n"
        % (name, description.strip(), today),
        encoding="utf-8",
    )
    return load(path, True)


def set_enabled(config: Config, name: str, enabled: bool) -> Extension:
    extension = find(config, name)
    if extension.enabled == enabled:
        return extension
    target_dir = config.extensions_dir if enabled else config.disabled_extensions_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    shutil.move(str(extension.path), str(target))
    return load(target, enabled)


def remove(config: Config, name: str) -> Path:
    """Move it out of the way, into state/ where it can still be recovered. Its history stays in git."""
    extension = find(config, name)
    trash = config.workspace / "state" / "removed-extensions"  # the home's state/, or the bot's own
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / ("%s-%s" % (name, datetime.now().strftime("%Y%m%d-%H%M%S")))
    shutil.move(str(extension.path), str(target))
    return target


# -- checking ----------------------------------------------------------------


def _files(path: Path) -> List[Path]:
    found = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found += [Path(root) / name for name in files]
    return sorted(found)


def check(config: Config, name: str, run_tests: bool = True) -> Dict[str, Any]:
    """Everything an extension must pass before Consiglieri relies on it."""
    extension = find(config, name)
    path = extension.path
    errors: List[str] = []
    warnings: List[str] = []

    try:
        validate_name(config, name)
    except ExtensionError as exc:
        errors.append(str(exc))
    manifest = _read_json(path / ".claude-plugin" / "plugin.json")
    if not manifest:
        errors.append(".claude-plugin/plugin.json is missing or not valid JSON")
    elif manifest.get("name") != name:
        errors.append("plugin.json name must be %r, the folder name" % name)
    if manifest and not manifest.get("version"):
        warnings.append("plugin.json has no version; bump it on every change")
    if not (path / "README.md").is_file():
        errors.append("README.md is missing: say what it does, what it needs, and what changed")
    if (path / "CLAUDE.md").exists():
        errors.append("CLAUDE.md inside an extension is not loaded; put instructions in a skill")
    if not (extension.skills or extension.commands or extension.agents or extension.runs_by_itself()):
        warnings.append("it has no skills, commands, agents, hooks or MCP servers yet")

    for skill in extension.skills:
        text = (path / "skills" / skill / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        from .home import parse_frontmatter

        fields = parse_frontmatter(text)
        if fields.get("name") not in (None, "", skill):
            errors.append("skills/%s/SKILL.md says name %r; it must match its folder" % (skill, fields.get("name")))
        if len(fields.get("description", "")) < 20:
            errors.append("skills/%s/SKILL.md needs a description that says what it does and when to use it" % skill)

    bin_dir = path / "bin"
    if bin_dir.is_dir():
        for item in sorted(bin_dir.iterdir()):
            if item.is_dir():
                errors.append("bin/%s is a folder; bin/ holds executables only" % item.name)
                continue
            if not _is_executable(item):
                errors.append("bin/%s is not executable (chmod +x)" % item.name)
            elif not item.read_bytes()[:2] == b"#!":
                errors.append("bin/%s has no #! line" % item.name)
            if shutil.which(item.name) and not str(shutil.which(item.name)).startswith(str(path)):
                warnings.append("bin/%s hides another command with the same name on PATH" % item.name)
        base_commands = {item.name for item in (BASE_PLUGIN / "bin").iterdir()} | {"alfred"}
        for command in extension.commands:
            if command in base_commands:
                errors.append("bin/%s would replace one of Consiglieri's own commands" % command)

    for item in _files(path):
        relative = item.relative_to(path)
        if item.is_symlink():
            errors.append("%s is a symlink; keep everything inside the folder" % relative)
            continue
        size = item.stat().st_size
        if size > MAX_FILE_BYTES:
            errors.append("%s is %d KB; keep large data in state/extensions/%s/" % (relative, size // 1024, name))
            continue
        try:
            text = item.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append("%s looks like it contains a %s; secrets never go in an extension" % (relative, label))
                break

    validator = _run_validator(config, path)
    if validator is not None:
        ok, output = validator
        if not ok:
            errors.append("claude plugin validate failed: %s" % output)
        elif "warning" in output.lower():
            warnings.append("claude plugin validate: %s" % output)

    tests = None
    if run_tests and (path / "tests").is_dir():
        tests = _run_tests(path)
        if not tests["ok"]:
            errors.append("tests failed")

    return {
        "ok": not errors,
        "name": name,
        "errors": errors,
        "warnings": warnings,
        "tests": tests,
        "needs_approval": extension.runs_by_itself(),
        "not_loaded_for_this_bot": held_back(config, extension),
        "extension": extension.to_json(),
    }


def _run_validator(config: Config, path: Path) -> Optional[tuple]:
    if not shutil.which(config.claude_bin):
        return None
    try:
        completed = subprocess.run(
            [config.claude_bin, "plugin", "validate", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    interesting = [line for line in lines if not line.startswith("The directory also runs")]
    return completed.returncode == 0, " | ".join(interesting[-6:])


def _run_tests(path: Path) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=str(path),
            # Tests import the extension's code as `lib.<module>` or from bin/ by path.
            env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(path), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "tests took more than 5 minutes"}
    return {"ok": completed.returncode == 0, "output": completed.stdout[-2000:]}
