"""Usage monitor: the skeleton for warnings about the Claude plan limits.

The pieces, and what each does today (see docs/USAGE_MONITOR_RESEARCH.md, section 9):

- `Window` and `Reading`: one look at the plan limits.
- `UsageSource` and `ClaudeUsageSource`: where readings come from. The Claude one
  asks `/usage` for its structured `usage_report` with `usage.py`'s helpers, and falls
  back to the text. It costs no quota (Claude Code answers `/usage` without the model).
- `Rule` and the six declared rules: what will decide when to warn. They have no
  logic yet and never return an alert.
- `UsageHistory`: the last readings, kept in a small JSON file.
- `MonitorSettings`: configuration, read from the environment and `.env`.
- `UsageMonitor` and `start()`: the entry point; `BotPool.start` calls it once per process.
  Its loop checks the agents' usage alerts (`usage_alerts.py`) with one reading per cycle.

The monitor is off unless `ALFRED_USAGE_MONITOR` is set. When it is off, `start()`
reads no other setting, builds nothing and returns None: no reading, no message, no wake-up.
When it is on, it reads usage only while some bot with the usage permission has an alert,
and the only thing it does is wake that bot. It sends the user nothing yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import usage, usage_alerts
from .config import Config
from .scheduler import KIND_EVENT, SchedulerStore, parse_duration, parse_time, to_utc_text, utc_now

log = logging.getLogger("alfred.usage_monitor")

ENABLE_SETTING = "ALFRED_USAGE_MONITOR"

# Meter kinds as Claude Code's structured usage report names them.
KIND_SESSION = "session"
KIND_WEEKLY_ALL = "weekly_all"
KIND_WEEKLY_SCOPED = "weekly_scoped"

SOURCE_TEXT = "text"  # parsed from the human-readable `/usage` text
SOURCE_REPORT = "usage_report"  # Claude Code's structured copy of `/usage` (documented, marked experimental)

# `/usage` as a stream, so the reply carries `usage_report`; without the user's hooks and MCP servers.
# Not --bare: bare mode does not read the OAuth login, and the plan usage needs it.
PROBE_FLAGS = ("--print", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
               "--setting-sources", "project", "--strict-mcp-config")


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
    def from_usage_report(cls, report: Dict[str, Any], taken_at: Optional[datetime] = None) -> "Reading":
        """Read the meters out of Claude Code's `usage_report`. Unknown kinds are kept under their own name."""
        taken_at = taken_at or utc_now()
        rate_limits = report.get("rate_limits") if isinstance(report, dict) else None
        if not isinstance(rate_limits, dict):
            return cls.failed("Claude Code returned no plan usage", SOURCE_REPORT, taken_at)
        windows: List[Window] = []
        for row in rate_limits.get("limits") or []:
            if not isinstance(row, dict) or row.get("percent") is None:
                continue
            kind = str(row.get("kind") or "unknown")
            scope_data = row.get("scope") or {}
            scope = None
            for part in ("model", "surface"):
                if isinstance(scope_data, dict) and isinstance(scope_data.get(part), dict):
                    scope = scope_data[part].get("display_name") or scope
            try:
                resets_at = _reset_time(row["resets_at"]) if row.get("resets_at") else None
                percent = float(row["percent"])
            except (TypeError, ValueError):
                continue
            windows.append(Window(
                kind=kind,
                label=_label_of(kind, scope),
                percent=percent,
                resets_at=resets_at,
                reset_text=resets_at.astimezone().strftime("%a %d %b %H:%M") if resets_at else "",
                severity=str(row.get("severity") or "unknown"),
                scope=scope,
            ))
        return cls(taken_at=taken_at, source=SOURCE_REPORT, windows=tuple(windows))

    def window(self, kind: str) -> Optional[Window]:
        return next((window for window in self.windows if window.kind == kind), None)

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


def _reset_time(text: Any) -> datetime:
    """ISO 8601 from the server, with any number of fraction digits (Python 3.9 takes only 3 or 6)."""
    return parse_time(re.sub(r"\.\d+", "", str(text).strip()))


def _label_of(kind: str, scope: Optional[str]) -> str:
    if kind == KIND_SESSION:
        return "Session"
    if kind == KIND_WEEKLY_ALL:
        return "Week (all models)"
    if kind == KIND_WEEKLY_SCOPED:
        return "Week (%s)" % (scope or "scoped")
    return kind.replace("_", " ").capitalize()


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
    """Claude Code's `/usage`: no model call, no quota spent.

    Asks for the structured `usage_report` and falls back to the text, read with the same pattern
    `usage.summarize` uses. Runs from `state/usage-probe/`, a folder with no project settings.
    """

    name = SOURCE_REPORT

    def __init__(self, config: Config, environ: Optional[Dict[str, str]] = None) -> None:
        self.config = config
        self.environ = dict(os.environ if environ is None else environ)

    async def read(self) -> Reading:
        found: Dict[str, Any] = {"report": None, "text": "", "result": None}

        async def on_event(event: Dict[str, Any]) -> None:
            if event.get("type") == "assistant":
                if isinstance(event.get("usage_report"), dict):
                    found["report"] = event["usage_report"]
                content = (event.get("message") or {}).get("content")
                if isinstance(content, str):
                    found["text"] = content
                elif isinstance(content, list):
                    found["text"] = "\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
            elif event.get("type") == "result" or "result" in event:
                found["result"] = event

        probe_dir = self.config.state_dir / "usage-probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        env = {key: value for key, value in self.environ.items() if key not in usage.INHERITED_SESSION_VARS}
        try:
            output = await usage.run_json_lines(
                [self.config.claude_bin] + list(PROBE_FLAGS),
                stdin_text="/usage", cwd=probe_dir, env=env, on_event=on_event, timeout=usage.USAGE_TIMEOUT,
            )
        except usage.BackendError as exc:
            return Reading.failed(str(exc), self.name)
        result = found["result"]
        if result is None or result.get("is_error"):
            detail = str((result or {}).get("result") or output.stderr or "no answer from claude").strip()
            return Reading.failed(detail.splitlines()[0], self.name)
        if result.get("total_cost_usd"):  # /usage never calls the model; if that changes, say so loudly
            log.warning("reading the usage cost %s USD; it should cost nothing", result.get("total_cost_usd"))
        if found["report"] is not None:
            return Reading.from_usage_report(found["report"])
        text = found["text"] or str(result.get("result") or "")
        reading = Reading.from_report(text)
        if not reading.windows and "rate limited" in text.lower():
            return Reading.failed(text.strip().splitlines()[0], SOURCE_TEXT)
        return reading


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

    @staticmethod
    def estimate(reading: Reading, now: datetime, window_share_percent: float) -> Optional[Tuple[float, float]]:
        """(full 5-hour windows the unused weekly share would take, 5-hour windows left before the weekly reset).

        `window_share_percent` is how much of the weekly limit one fully used 5-hour window takes
        (`MonitorSettings.window_share_percent`). None when the reading has no weekly reset time.
        The rule will compare the two numbers; it does not act on them yet.
        """
        week = reading.window(KIND_WEEKLY_ALL)
        if week is None or week.resets_at is None or window_share_percent <= 0:
            return None
        unused = max(0.0, 100.0 - week.percent)
        left = max(0.0, (week.resets_at - now).total_seconds())
        return unused / window_share_percent, left / (5 * 3600)


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
        "it can't read usage or set alerts until you put %s=1 in .env and restart Consiglieri."
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
    # ALFRED_USAGE_WAKE_INTERVAL: the least time between two usage-alert wake-ups of one bot. Waking costs usage.
    wake_interval_seconds: int = 60 * 60
    # ALFRED_USAGE_WINDOW_SHARE: percent of the weekly limit one fully used 5-hour window takes. Not published by
    # Anthropic; 8 is a rounded community median for Pro (and Max, which official pages put at 5x Pro in both
    # limits), measured in November 2026. Sources and range: docs/USAGE_MONITOR_RESEARCH.md, section 10.
    window_share_percent: float = 8.0
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
            wake_interval_seconds=_setting(config, "ALFRED_USAGE_WAKE_INTERVAL", defaults.wake_interval_seconds, _poll_seconds),
            window_share_percent=_setting(config, "ALFRED_USAGE_WINDOW_SHARE", defaults.window_share_percent, _share),
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


def _share(text: str) -> float:
    value = float(text.strip().rstrip("%"))
    if not 0.5 <= value <= 100:
        raise ValueError("a share of the weekly limit between 0.5 and 100")
    return value


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
    """Reads the plan usage once per cycle and wakes the bots whose usage alerts hold.

    The six warning rules for the user are still only declared. A cycle reads nothing when no bot
    has an alert to check, and never calls the model: the only usage it spends is the agent turn a
    fired alert starts, at most one per bot per `wake_interval_seconds`.
    """

    def __init__(
        self,
        settings: MonitorSettings,
        source: UsageSource,
        history: UsageHistory,
        rules: Optional[Sequence[Rule]] = None,
        *,
        config: Optional[Config] = None,
        scheduler: Optional[SchedulerStore] = None,
        first_delay: float = 30.0,
    ) -> None:
        self.settings = settings
        self.source = source
        self.history = history
        self.rules = list(default_rules() if rules is None else rules)
        self.config = config
        self.scheduler = scheduler
        self.first_delay = first_delay
        self._task: Optional["asyncio.Future[None]"] = None

    def announce(self) -> None:
        log.info(
            "usage monitor is on: checks agents' usage alerts every %dm (one /usage reading, no model call); "
            "%d warning rules declared, none active",
            self.settings.poll_seconds // 60, len(self.rules),
        )

    # -- the loop -------------------------------------------------------------

    def start_loop(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run(self) -> None:
        await asyncio.sleep(self.first_delay)
        while True:
            try:
                await self.cycle()
            except Exception:  # one bad cycle must not end the monitor
                log.exception("usage monitor cycle failed")
            await asyncio.sleep(self.settings.poll_seconds)

    def targets(self) -> List[Tuple[Any, usage_alerts.AlertStore, List[usage_alerts.Alert]]]:
        """Bots that are on, have the usage permission and have an alert left to check."""
        if self.config is None:
            return []
        from .bots import BotRegistry

        found = []
        for record in BotRegistry(self.config).list():
            if not record.enabled or not record.allows("usage"):
                continue
            store = usage_alerts.AlertStore.for_bot(self.config, record.bot_id)
            alerts = [alert for alert in store.alerts() if not alert.spent]
            if alerts:
                found.append((record, store, alerts))
        return found

    async def cycle(self, now: Optional[datetime] = None) -> Optional[Reading]:
        """One pass: one reading for every bot's alerts. Returns the reading, or None when none was needed."""
        targets = self.targets()
        if not targets:
            return None
        reading = await self.source.read()
        now = now or utc_now()
        self.history.append(reading, now=now)
        if not reading.ok:
            log.warning("could not read the plan usage: %s", reading.error)
            return reading
        for record, store, alerts in targets:
            firings = usage_alerts.due(alerts, reading, now)
            # Only the bot's own chats, unless it may write to others: the same rule as when it was set.
            firings = [item for item in firings if record.allows("contact") or item.alert.chat_id in record.own_chats()]
            if not firings:
                continue
            if not usage_alerts.wake_allowed(store, now, self.settings.wake_interval_seconds):
                log.info("%s: %d usage alert(s) hold; waiting for its wake interval", record.handle, len(firings))
                continue
            fired = usage_alerts.mark_fired(store, firings, now)
            self._wake(record.bot_id, fired, reading, now)
        return reading

    def _wake(self, bot_id: int, fired: List[usage_alerts.Alert], reading: Reading, now: datetime) -> None:
        if not fired or self.scheduler is None:
            return
        by_chat: Dict[Tuple[int, Optional[int]], List[usage_alerts.Alert]] = {}
        for alert in fired:
            by_chat.setdefault((alert.chat_id, alert.thread_id), []).append(alert)
        for (chat_id, thread_id), group in by_chat.items():
            self.scheduler.add(
                chat_id=chat_id, due=now, text=usage_alerts.event_text(group, reading, now), kind=KIND_EVENT,
                user_id=group[0].user_id, username=group[0].username, bot_id=bot_id, thread_id=thread_id,
            )
            log.info("usage alert(s) %s met: waking bot %s", ", ".join(alert.id for alert in group), bot_id)


def start(
    config: Config, environ: Optional[Dict[str, str]] = None, *, scheduler: Optional[SchedulerStore] = None,
) -> Optional[UsageMonitor]:
    """Called once per process, by `BotPool.start`, with Alfred's own configuration and the shared scheduler.

    Off by default: then this reads nothing else and returns None. A created bot's configuration
    (`config.bot_id` set) never starts one, because the plan limits belong to the Claude account that
    every bot of the process shares, so one monitor covers them all. Without a scheduler the monitor is
    built but its loop is not started.
    """
    if config.bot_id or not is_enabled(config):
        return None
    settings = MonitorSettings.from_config(config)
    monitor = UsageMonitor(settings, ClaudeUsageSource(config, environ), UsageHistory(settings.history_path),
                           config=config, scheduler=scheduler)
    monitor.announce()
    if scheduler is not None:
        monitor.start_loop()
    return monitor
