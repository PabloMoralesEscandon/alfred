"""Usage monitor: the skeleton for warnings about the Claude plan limits.

Nothing here acts yet. The pieces are in place so the behaviour can be added one
step at a time (see docs/USAGE_MONITOR_RESEARCH.md, section 9):

- `Window` and `Reading`: one look at the plan limits.
- `UsageSource` and `ClaudeUsageSource`: where readings come from. The Claude one
  reuses `usage.fetch_report`, which costs no quota (Claude Code answers `/usage`
  without calling the model).
- `Rule` and the six declared rules: what will decide when to warn. They have no
  logic yet and never return an alert.
- `UsageHistory`: the last readings, kept in a small JSON file.
- `MonitorSettings`: configuration, read from the environment and `.env`.
- `UsageMonitor` and `start()`: the entry point; `BotPool.start` calls it once per process.

The monitor is off unless `ALFRED_USAGE_MONITOR` is set. When it is off, `start()`
reads no other setting, builds nothing and returns None. When it is on, it only
logs one line: it reads no usage, sends no messages and schedules nothing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import usage
from .config import Config
from .scheduler import parse_duration, parse_time, to_utc_text, utc_now

log = logging.getLogger("alfred.usage_monitor")

ENABLE_SETTING = "ALFRED_USAGE_MONITOR"

# Meter kinds as Claude Code's structured usage report names them.
KIND_SESSION = "session"
KIND_WEEKLY_ALL = "weekly_all"
KIND_WEEKLY_SCOPED = "weekly_scoped"

SOURCE_TEXT = "text"  # parsed from the human-readable `/usage` text


# -- one reading -------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One usage meter: the 5-hour session, the whole week, or the week for one model."""

    kind: str
    label: str
    percent: float
    resets_at: Optional[datetime] = None  # aware UTC; None when the source only gave text
    reset_text: str = ""  # the reset as the source wrote it, for display
    severity: str = "unknown"  # the server's reading: normal, warning, critical
    scope: Optional[str] = None  # the model or surface of a scoped meter, such as "Fable"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "percent": self.percent,
            "resets_at": to_utc_text(self.resets_at) if self.resets_at else None,
            "reset_text": self.reset_text,
            "severity": self.severity,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Window":
        resets_at = data.get("resets_at")
        return cls(
            kind=str(data.get("kind") or ""),
            label=str(data.get("label") or ""),
            percent=float(data.get("percent") or 0),
            resets_at=parse_time(resets_at) if resets_at else None,
            reset_text=str(data.get("reset_text") or ""),
            severity=str(data.get("severity") or "unknown"),
            scope=data.get("scope"),
        )


@dataclass(frozen=True)
class Reading:
    """Every meter at one moment, or the reason there are none."""

    taken_at: datetime
    source: str
    windows: Tuple[Window, ...] = ()
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def from_report(cls, report: str, taken_at: Optional[datetime] = None) -> "Reading":
        """Read the meters out of the `/usage` text, with the same pattern `usage.summarize` uses."""
        windows: List[Window] = []
        for raw in report.splitlines():
            match = usage.LIMIT_LINE.match(raw.strip())
            if not match:
                continue
            label = match.group("label").strip()
            kind, scope = _kind_of(label)
            windows.append(
                Window(
                    kind=kind,
                    label=label[0].upper() + label[1:],
                    percent=float(match.group("percent")),
                    reset_text=(match.group("reset") or "").strip(),
                    scope=scope,
                )
            )
        return cls(taken_at=taken_at or utc_now(), source=SOURCE_TEXT, windows=tuple(windows))

    @classmethod
    def failed(cls, error: str, source: str, taken_at: Optional[datetime] = None) -> "Reading":
        return cls(taken_at=taken_at or utc_now(), source=source, error=error)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "taken_at": to_utc_text(self.taken_at),
            "source": self.source,
            "windows": [window.to_dict() for window in self.windows],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Reading":
        return cls(
            taken_at=parse_time(data["taken_at"]),
            source=str(data.get("source") or ""),
            windows=tuple(Window.from_dict(item) for item in data.get("windows") or []),
            error=data.get("error"),
        )


def _kind_of(label: str) -> Tuple[str, Optional[str]]:
    """`session` -> session; `week (all models)` -> weekly_all; `week (Fable)` -> weekly_scoped for Fable."""
    lowered = label.lower()
    if lowered == "session":
        return KIND_SESSION, None
    if lowered.startswith("week"):
        inside = label[label.find("(") + 1:label.rfind(")")].strip() if "(" in label else ""
        if not inside or inside.lower() == "all models":
            return KIND_WEEKLY_ALL, None
        return KIND_WEEKLY_SCOPED, inside
    return lowered, None  # a meter this version does not know: keep it under its own name


# -- where readings come from ------------------------------------------------


class UsageSource:
    """Something that can take a reading. Implementations must not spend plan quota."""

    name = "source"

    async def read(self) -> Reading:
        raise NotImplementedError


class ClaudeUsageSource(UsageSource):
    """Claude Code's `/usage`, through `usage.fetch_report`: no model call, no quota spent."""

    name = SOURCE_TEXT

    def __init__(self, config: Config, environ: Optional[Dict[str, str]] = None) -> None:
        self.config = config
        self.environ = dict(os.environ if environ is None else environ)

    async def read(self) -> Reading:
        try:
            report = await usage.fetch_report(self.config, self.environ)
        except usage.UsageUnavailable as exc:
            return Reading.failed(str(exc), self.name)
        return Reading.from_report(report)


# -- warning rules -----------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """A warning a rule wants sent. `key` keeps the same warning from being sent twice."""

    rule: str
    key: str
    text: str
    urgent: bool = False


@dataclass(frozen=True)
class RuleContext:
    """What a rule looks at: the reading just taken and the ones before it."""

    now: datetime
    reading: Reading
    history: Sequence[Reading]
    settings: "MonitorSettings"


class Rule:
    """One reason to warn. `evaluate` returns the alerts the context calls for."""

    name = "rule"
    summary = ""
    implemented = False

    def evaluate(self, context: RuleContext) -> List[Alert]:
        return []


class ThresholdRule(Rule):
    name = "threshold"
    summary = "A meter crosses a warning percentage, or the server marks it warning or critical."


class ProjectionRule(Rule):
    name = "projection"
    summary = "At the recent pace, a meter will reach 100% before it resets."


class LimitReachedRule(Rule):
    name = "limit_reached"
    summary = "A meter is used up; scheduled tasks wait for its reset."


class ResetRule(Rule):
    name = "reset"
    summary = "A meter that was nearly or fully used has reset."


class SpareSessionRule(Rule):
    name = "spare_session"
    summary = "The 5-hour window resets soon with plenty unused, and the week has room."


class SpareWeeklyRule(Rule):
    name = "spare_weekly"
    summary = "The week resets soon with plenty unused."


def default_rules() -> List[Rule]:
    return [ThresholdRule(), ProjectionRule(), LimitReachedRule(), ResetRule(), SpareSessionRule(), SpareWeeklyRule()]


# -- history -----------------------------------------------------------------


class UsageHistory:
    """Recent readings in a JSON file, oldest first. Nothing touches the disk until a method is called."""

    def __init__(self, path: Path, keep: timedelta = timedelta(hours=24)) -> None:
        self.path = Path(path)
        self.keep = keep

    def readings(self) -> List[Reading]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        readings: List[Reading] = []
        for item in data.get("readings") or []:
            try:
                readings.append(Reading.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue  # one damaged entry must not lose the rest
        return readings

    def latest(self) -> Optional[Reading]:
        readings = self.readings()
        return readings[-1] if readings else None

    def append(self, reading: Reading, now: Optional[datetime] = None) -> None:
        cutoff = (now or utc_now()) - self.keep
        readings = [item for item in self.readings() if item.taken_at >= cutoff] + [reading]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({"readings": [item.to_dict() for item in readings]}, indent=1), encoding="utf-8")
        os.replace(str(temporary), str(self.path))


# -- configuration -----------------------------------------------------------


def is_enabled(config: Config) -> bool:
    return config.get_bool(ENABLE_SETTING, False)


def permission_note(config: Config, record: Any) -> str:
    """What the panel says about a bot's usage permission when the monitor itself is off; empty otherwise."""
    if record is None or not record.allows("usage") or is_enabled(config):
        return ""
    return (
        "ℹ️ Plan usage is on for %s, but the usage monitor is off (%s=0), so the permission has no effect: "
        "it can't read usage or set alerts until you put %s=1 in .env and restart Alfred."
        % (record.handle, ENABLE_SETTING, ENABLE_SETTING)
    )


@dataclass
class MonitorSettings:
    """Monitor settings. Only `enabled` has an effect today; the rest are for the rules to come."""

    enabled: bool = False
    chat_id: Optional[int] = None  # ALFRED_USAGE_CHAT_ID, or the only numeric id in ALFRED_ALLOWED_USERS
    poll_seconds: int = 15 * 60  # ALFRED_USAGE_POLL
    warn_percents: Tuple[int, ...] = (80, 95)  # ALFRED_USAGE_WARN
    cap_percent: Optional[int] = 95  # ALFRED_USAGE_CAP; None never pauses scheduled tasks
    quiet_hours: Optional[Tuple[int, int]] = (23, 8)  # ALFRED_QUIET_HOURS, local start and end hour
    spare: bool = True  # ALFRED_USAGE_SPARE
    autorun: bool = False  # ALFRED_USAGE_AUTORUN
    history_path: Path = field(default_factory=lambda: Path("usage.json"))

    MIN_POLL_SECONDS = 5 * 60

    @classmethod
    def from_config(cls, config: Config) -> "MonitorSettings":
        """Read every setting. A value that does not parse keeps its default and is logged."""
        defaults = cls()
        return cls(
            enabled=is_enabled(config),
            chat_id=_chat_id(config),
            poll_seconds=_setting(config, "ALFRED_USAGE_POLL", defaults.poll_seconds, _poll_seconds),
            warn_percents=_setting(config, "ALFRED_USAGE_WARN", defaults.warn_percents, _percents),
            cap_percent=_setting(config, "ALFRED_USAGE_CAP", defaults.cap_percent, _optional_percent),
            quiet_hours=_setting(config, "ALFRED_QUIET_HOURS", defaults.quiet_hours, _hours),
            spare=config.get_bool("ALFRED_USAGE_SPARE", defaults.spare),
            autorun=config.get_bool("ALFRED_USAGE_AUTORUN", defaults.autorun),
            history_path=config.state_dir / "usage.json",
        )


def _setting(config: Config, name: str, default: Any, parse: Any) -> Any:
    text = config.get(name)
    if not text:
        return default
    try:
        return parse(text)
    except ValueError as exc:
        log.warning("ignoring %s=%r (%s); using %r", name, text, exc, default)
        return default


def _chat_id(config: Config) -> Optional[int]:
    text = config.get("ALFRED_USAGE_CHAT_ID")
    if text.lstrip("-").isdigit():
        return int(text)
    ids = [entry for entry in config.allowed_users() if entry.isdigit()]
    return int(ids[0]) if len(ids) == 1 else None  # with several people allowed, it must be set


def _poll_seconds(text: str) -> int:
    seconds = parse_duration(text)
    if seconds < MonitorSettings.MIN_POLL_SECONDS:
        raise ValueError("must be at least %dm" % (MonitorSettings.MIN_POLL_SECONDS // 60))
    return seconds


def _percent(text: str) -> int:
    value = int(text.strip().rstrip("%"))
    if not 0 < value <= 100:
        raise ValueError("a percentage must be between 1 and 100")
    return value


def _percents(text: str) -> Tuple[int, ...]:
    values = sorted({_percent(part) for part in text.split(",") if part.strip()})
    if not values:
        raise ValueError("no percentages given")
    return tuple(values)


def _optional_percent(text: str) -> Optional[int]:
    return None if text.strip().lower() in ("off", "none", "0") else _percent(text)


def _hours(text: str) -> Optional[Tuple[int, int]]:
    if text.strip().lower() in ("off", "none"):
        return None
    start, separator, end = text.partition("-")
    if not separator:
        raise ValueError("give a range of hours such as 23-8")
    hours = (int(start), int(end))
    if not all(0 <= hour <= 23 for hour in hours):
        raise ValueError("hours must be between 0 and 23")
    return hours


# -- the monitor -------------------------------------------------------------


class UsageMonitor:
    """Holds the pieces. It does nothing yet beyond saying it is there."""

    def __init__(
        self,
        settings: MonitorSettings,
        source: UsageSource,
        history: UsageHistory,
        rules: Optional[Sequence[Rule]] = None,
    ) -> None:
        self.settings = settings
        self.source = source
        self.history = history
        self.rules = list(default_rules() if rules is None else rules)

    def announce(self) -> None:
        log.info(
            "usage monitor is on but not implemented yet: it reads no usage and sends nothing (%d rules declared)",
            len(self.rules),
        )


def start(config: Config, environ: Optional[Dict[str, str]] = None) -> Optional[UsageMonitor]:
    """Called once per process, by `BotPool.start`, with Alfred's own configuration.

    Off by default: then this reads nothing else and returns None. A created bot's configuration
    (`config.bot_id` set) never starts one, because the plan limits belong to the Claude account that
    every bot of the process shares, so one monitor covers them all.
    """
    if config.bot_id or not is_enabled(config):
        return None
    settings = MonitorSettings.from_config(config)
    monitor = UsageMonitor(settings, ClaudeUsageSource(config, environ), UsageHistory(settings.history_path))
    monitor.announce()
    return monitor
