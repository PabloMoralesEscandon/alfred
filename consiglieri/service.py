"""Keep `consiglieri run` alive: a per-user background service that starts at login and restarts on exit.

macOS uses a launchd LaunchAgent, Linux a systemd user unit. Both are
per-user on purpose: Claude Code's login, the Orca app and Chrome all belong
to the logged-in user, so a system-wide daemon could not reach them.

Installs from before the rename run as `com.consiglieri.bot` / `consiglieri.service`. Until `install` replaces it,
`uninstall`, `restart` and `status` act on whichever of the two is installed, and `install` always removes
the old one first: two services with the same bot token would fight over Telegram's updates.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import REPO_ROOT, Config

LABEL = "com.consiglieri.bot"
SYSTEMD_UNIT = "consiglieri.service"
LEGACY_LABEL = "com.alfred.bot"  # rename-compat
LEGACY_SYSTEMD_UNIT = "alfred.service"  # rename-compat
LEGACY_LOG = "alfred.log"  # rename-compat: where the old service writes, in state/
RESTART_DELAY_SECONDS = 15


class ServiceError(RuntimeError):
    pass


def log_path(config: Config) -> Path:
    return config.state_dir / "consiglieri.log"


def program() -> List[str]:
    return [sys.executable, "-m", "consiglieri", "run"]


def service_environment(config: Config) -> Dict[str, str]:
    """What the service needs that a login shell would normally provide.

    launchd and systemd start processes with a bare PATH, so the one in use
    now is recorded; it is how `claude`, `codex` and `orca` are found.
    """
    seen: List[str] = []
    for entry in config.environ.get("PATH", "").split(os.pathsep):
        if entry and entry not in seen:
            seen.append(entry)
    return {
        "PATH": os.pathsep.join(seen) or "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "CONSIGLIERI_HOME": str(config.home),
    }


def preflight(config: Config) -> List[str]:
    """Reasons the bot would crash-loop if started now. Empty means good to go."""
    problems: List[str] = []
    try:
        config.telegram_token
    except RuntimeError as exc:
        problems.append(str(exc))
    if not config.allowed_users():
        problems.append("CONSIGLIERI_ALLOWED_USERS is empty, so nobody could talk to the bot.")
    backend_bin = config.codex_bin if config.default_backend == "codex" else config.claude_bin
    if not shutil.which(backend_bin, path=service_environment(config)["PATH"]):
        problems.append("`%s` was not found on PATH." % backend_bin)
    return problems


# -- launchd (macOS) -----------------------------------------------------------


def launchd_plist_path(label: str = LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % label)


def launchd_plist(config: Config, label: str = LABEL, arguments: Optional[List[str]] = None) -> bytes:
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": arguments or program(),
            "WorkingDirectory": str(REPO_ROOT),
            "EnvironmentVariables": service_environment(config),
            "RunAtLoad": True,
            "KeepAlive": True,  # restart whenever it exits, for any reason
            "ThrottleInterval": RESTART_DELAY_SECONDS,
            "StandardOutPath": str(log_path(config)),
            "StandardErrorPath": str(log_path(config)),
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)


def _launchd_target(label: str) -> str:
    return "gui/%d/%s" % (os.getuid(), label)


def launchd_install(config: Config, label: str = LABEL, arguments: Optional[List[str]] = None) -> Path:
    path = launchd_plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path(config).parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", _launchd_target(label))  # replace a previous install; fails harmlessly if none
    path.write_bytes(launchd_plist(config, label, arguments))
    result = _launchctl("bootstrap", "gui/%d" % os.getuid(), str(path))
    if result.returncode != 0:
        raise ServiceError("launchctl could not load the service: %s" % result.stdout.strip())
    _launchctl("enable", _launchd_target(label))
    return path


def launchd_uninstall(label: str = LABEL) -> bool:
    path = launchd_plist_path(label)
    _launchctl("bootout", _launchd_target(label))
    existed = path.exists()
    if existed:
        path.unlink()
    return existed


def launchd_status(label: str = LABEL) -> Dict[str, Optional[str]]:
    result = _launchctl("print", _launchd_target(label))
    if result.returncode != 0:
        return {"installed": "yes" if launchd_plist_path(label).exists() else "no", "state": "not loaded", "pid": None}

    def field(name: str) -> Optional[str]:
        match = re.search(r"^\s*%s = (.+)$" % re.escape(name), result.stdout, re.MULTILINE)
        return match.group(1).strip() if match else None

    return {"installed": "yes", "state": field("state") or "unknown", "pid": field("pid"), "last_exit": field("last exit code")}


def launchd_restart(label: str = LABEL) -> None:
    result = _launchctl("kickstart", "-k", _launchd_target(label))
    if result.returncode != 0:
        raise ServiceError("service is not installed (%s)" % result.stdout.strip())


# -- systemd (Linux) -----------------------------------------------------------


def systemd_unit_path(unit: str = SYSTEMD_UNIT) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / unit


def active_label() -> str:
    """The launchd job to act on: the new one if it is installed, else an old one that is, else the new one."""
    if not launchd_plist_path(LABEL).exists() and launchd_plist_path(LEGACY_LABEL).exists():
        return LEGACY_LABEL
    return LABEL


def active_unit() -> str:
    if not systemd_unit_path(SYSTEMD_UNIT).exists() and systemd_unit_path(LEGACY_SYSTEMD_UNIT).exists():
        return LEGACY_SYSTEMD_UNIT
    return SYSTEMD_UNIT


def retire_legacy() -> bool:
    """Stop and remove the service an install from before the rename left. Whether there was one."""
    if platform() == "launchd":
        return launchd_uninstall(LEGACY_LABEL)
    path = systemd_unit_path(LEGACY_SYSTEMD_UNIT)
    _systemctl("disable", "--now", LEGACY_SYSTEMD_UNIT)
    existed = path.exists()
    if existed:
        path.unlink()
        _systemctl("daemon-reload")
    return existed


def systemd_unit(config: Config) -> str:
    def quote(value: str) -> str:
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')

    environment = "\n".join(
        "Environment=%s" % quote("%s=%s" % item) for item in sorted(service_environment(config).items())
    )
    return (
        "[Unit]\n"
        "Description=Consiglieri Telegram agents\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "ExecStart=%s\n"
        "WorkingDirectory=%s\n"
        "%s\n"
        "Restart=always\n"
        "RestartSec=%d\n"
        "StandardOutput=append:%s\n"
        "StandardError=append:%s\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
        % (
            " ".join(quote(part) for part in program()),
            quote(str(REPO_ROOT)),
            environment,
            RESTART_DELAY_SECONDS,
            log_path(config),
            log_path(config),
        )
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False
    )


# -- platform dispatch ---------------------------------------------------------


def platform() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return "systemd"
    raise ServiceError("no supported service manager here; run `consiglieri run` under your own supervisor")


def install(config: Config) -> Tuple[Path, List[str]]:
    """Install and start the service. Returns the unit file and any follow-up notes."""
    problems = preflight(config)
    if problems:
        raise ServiceError("not installing, the bot would not start:\n- " + "\n- ".join(problems))
    notes: List[str] = []
    if retire_legacy():  # never two services with the same token
        notes.append("Removed the service installed under the old name.")
    if platform() == "launchd":
        path = launchd_install(config)
        notes.append("It starts when you log in. For it to come back after a power cut, turn on automatic login.")
    else:
        path = systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        log_path(config).parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(config), encoding="utf-8")
        _systemctl("daemon-reload")
        result = _systemctl("enable", "--now", SYSTEMD_UNIT)
        if result.returncode != 0:
            raise ServiceError("systemctl could not start the service: %s" % result.stdout.strip())
        notes.append("To keep it running while you are logged out: sudo loginctl enable-linger %s" % os.environ.get("USER", "$USER"))
    return path, notes


def uninstall() -> bool:
    if platform() == "launchd":
        return launchd_uninstall(active_label())
    unit = active_unit()
    path = systemd_unit_path(unit)
    _systemctl("disable", "--now", unit)
    existed = path.exists()
    if existed:
        path.unlink()
        _systemctl("daemon-reload")
    return existed


def restart() -> None:
    if platform() == "launchd":
        launchd_restart(active_label())
    elif _systemctl("restart", active_unit()).returncode != 0:
        raise ServiceError("service is not installed")


def status() -> str:
    if platform() == "launchd":
        label = active_label()
        info = launchd_status(label)
        if info["installed"] == "no":
            return "not installed"
        if info["state"] == "running":
            return "running (pid %s)" % info.get("pid") + _legacy_note(label == LEGACY_LABEL)
        text = "installed, %s" % info["state"]
        if info.get("last_exit") not in (None, "(never exited)"):
            text += ", last exit code %s" % info["last_exit"]
        return text + _legacy_note(label == LEGACY_LABEL)
    unit = active_unit()
    if not systemd_unit_path(unit).exists():
        return "not installed"
    return (_systemctl("is-active", unit).stdout.strip() or "unknown") + _legacy_note(unit == LEGACY_SYSTEMD_UNIT)


def _legacy_note(legacy: bool) -> str:
    return " (installed under the old name; `consiglieri migrate`, then `consiglieri service install`)" if legacy else ""


def tail_log(config: Config, lines: int = 40) -> str:
    path = log_path(config)
    if not path.exists() and (config.state_dir / LEGACY_LOG).exists():
        path = config.state_dir / LEGACY_LOG  # rename-compat: the old service's log, until it is reinstalled
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log yet at %s)" % path
    return "\n".join(content[-lines:])
