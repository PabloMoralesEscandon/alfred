"""Usage alerts for agents: "wake me when this is true about the Claude plan usage".

A bot registers an alert with `alfred usage-alert add --when "<condition>"`. The usage monitor, which
runs once per process in the bot pool, takes one reading per cycle and checks every bot's alerts against
it. When a condition holds, the bot that set the alert, and no other, is woken with an event that says
which condition held, what was read and when the windows reset.

Conditions compare four values, joined with `and`, `or`, `not` and parentheses (`and` binds tighter):

    session.used   percent of the 5-hour window used          session.used < 10
    session.left   time until the 5-hour window resets        session.left < 1h
    week.used      percent of the weekly limit (all models)   week.used >= 80%
    week.left      time until the weekly limit resets         week.left <= 2d

Operators: < <= > >= = == !=. Percentages are plain numbers, with or without %. Times take a unit:
s, m, h, d or w (90m, 1h, 2d). `reset` is accepted for `left`, and `5h` for `session`.
A value that was not read (no 5-hour window running, a failed reading) makes its comparison false.

An alert fires once and is then spent, unless it was added with --repeat: then it fires at most once
per window (the windows its condition names), and again in the next one. What was fired is written to
disk before the bot is woken, so a restart does not fire it again.

Waking a bot costs plan usage (one agent turn with its whole context), unlike reading `/usage`.
So one bot is woken at most once per `ALFRED_USAGE_WAKE_INTERVAL`: alerts that hold sooner wait,
unspent, for the next cycle after it, and alerts that hold together arrive in one event.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .scheduler import parse_duration, parse_time, to_utc_text, utc_now

try:  # pragma: no cover - always there on macOS and Linux
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

MAX_ALERTS_PER_BOT = 20
MAX_CONDITION_LENGTH = 300
MAX_NOTE_LENGTH = 500
KEEP_FIRED_KEYS = 20
RESET_ROUNDING = timedelta(minutes=5)  # the server's reset times wobble by a fraction of a second

WINDOW_KINDS = {"session": "session", "5h": "session", "week": "weekly_all", "weekly": "weekly_all"}
FIELDS = {"used": "used", "left": "left", "reset": "left"}
OPERATORS = ("<=", ">=", "==", "!=", "<", ">", "=")


class ConditionError(ValueError):
    pass


# -- conditions -------------------------------------------------------------

TOKEN = re.compile(r"\s*(?:(?P<paren>[()])|(?P<op><=|>=|==|!=|<|>|=)|(?P<word>[A-Za-z0-9_.%]+|&&|\|\|))")


@dataclass(frozen=True)
class Comparison:
    window: str  # session | weekly_all
    field: str  # used | left
    op: str
    value: float  # percent, or seconds for `left`
    text: str

    def check(self, values: Dict[str, Optional[float]]) -> bool:
        actual = values.get("%s.%s" % (self.window, self.field))
        if actual is None:
            return False
        return {
            "<": actual < self.value, "<=": actual <= self.value, ">": actual > self.value,
            ">=": actual >= self.value, "=": actual == self.value, "==": actual == self.value,
            "!=": actual != self.value,
        }[self.op]


@dataclass(frozen=True)
class Node:
    kind: str  # and | or | not | cmp
    children: Tuple["Node", ...] = ()
    comparison: Optional[Comparison] = None

    def check(self, values: Dict[str, Optional[float]]) -> bool:
        if self.kind == "cmp":
            assert self.comparison is not None
            return self.comparison.check(values)
        if self.kind == "not":
            return not self.children[0].check(values)
        if self.kind == "and":
            return all(child.check(values) for child in self.children)
        return any(child.check(values) for child in self.children)

    def comparisons(self) -> List[Comparison]:
        if self.comparison is not None:
            return [self.comparison]
        return [item for child in self.children for item in child.comparisons()]

    def windows(self) -> List[str]:
        return sorted({item.window for item in self.comparisons()})


def _tokens(text: str) -> List[str]:
    tokens: List[str] = []
    position = 0
    text = text.strip()
    while position < len(text):
        match = TOKEN.match(text, position)
        if not match or match.end() == position:
            raise ConditionError("can't read %r" % text[position:position + 20])
        tokens.append(match.group(match.lastgroup))
        position = match.end()
    return tokens


def parse(text: str) -> Node:
    """Turn a condition such as `session.left < 1h and session.used = 0` into a tree. Raises ConditionError."""
    if not text.strip():
        raise ConditionError("the condition is empty")
    if len(text) > MAX_CONDITION_LENGTH:
        raise ConditionError("the condition is longer than %d characters" % MAX_CONDITION_LENGTH)
    tokens = _tokens(text)
    position = [0]

    def peek() -> str:
        return tokens[position[0]].lower() if position[0] < len(tokens) else ""

    def take() -> str:
        if position[0] >= len(tokens):
            raise ConditionError("the condition ends too early")
        token = tokens[position[0]]
        position[0] += 1
        return token

    def expression() -> Node:
        children = [term()]
        while peek() in ("or", "||"):
            take()
            children.append(term())
        return children[0] if len(children) == 1 else Node("or", tuple(children))

    def term() -> Node:
        children = [factor()]
        while peek() in ("and", "&&"):
            take()
            children.append(factor())
        return children[0] if len(children) == 1 else Node("and", tuple(children))

    def factor() -> Node:
        if peek() == "not":
            take()
            return Node("not", (factor(),))
        if peek() == "(":
            take()
            node = expression()
            if take() != ")":
                raise ConditionError("a parenthesis is not closed")
            return node
        return comparison()

    def comparison() -> Node:
        name = take()
        window, _, field_name = name.lower().partition(".")
        if window not in WINDOW_KINDS or field_name not in FIELDS:
            raise ConditionError(
                "unknown value %r; use session.used, session.left, week.used or week.left" % name)
        op = take()
        if op not in OPERATORS:
            raise ConditionError("expected a comparison (< <= > >= = !=) after %s, got %r" % (name, op))
        raw = take()
        field_name = FIELDS[field_name]
        if field_name == "used":
            try:
                value = float(raw.rstrip("%"))
            except ValueError:
                raise ConditionError("%s compares with a percentage, such as 50 or 50%%, not %r" % (name, raw)) from None
            if not 0 <= value <= 100:
                raise ConditionError("a percentage goes from 0 to 100, not %r" % raw)
        else:
            try:
                value = float(parse_duration(raw))
            except ValueError:
                if raw.strip() == "0":
                    value = 0.0
                else:
                    raise ConditionError("%s compares with a time such as 45m, 1h or 2d, not %r" % (name, raw)) from None
        text_form = "%s.%s %s %s" % ("session" if WINDOW_KINDS[window] == "session" else "week", field_name, op, raw)
        return Node("cmp", comparison=Comparison(WINDOW_KINDS[window], field_name, op, value, text_form))

    node = expression()
    if position[0] != len(tokens):
        raise ConditionError("unexpected %r" % tokens[position[0]])
    return node


def values_of(reading: Any, now: datetime) -> Dict[str, Optional[float]]:
    """What a condition can compare, from one reading: percent used and seconds left, per window."""
    values: Dict[str, Optional[float]] = {}
    for kind in ("session", "weekly_all"):
        window = reading.window(kind) if reading is not None and reading.ok else None
        values["%s.used" % kind] = window.percent if window is not None else None
        left = (window.resets_at - now).total_seconds() if window is not None and window.resets_at else None
        values["%s.left" % kind] = max(0.0, left) if left is not None else None
    return values


def window_key(node: Node, reading: Any) -> str:
    """Which windows a firing belongs to: the reset times of the windows the condition names."""
    parts = []
    for kind in node.windows():
        window = reading.window(kind) if reading is not None else None
        if window is None or window.resets_at is None:
            parts.append("%s:none" % kind)
            continue
        seconds = RESET_ROUNDING.total_seconds()
        rounded = round(window.resets_at.timestamp() / seconds) * seconds
        parts.append("%s:%d" % (kind, rounded))
    return ",".join(parts)


# -- alerts on disk -----------------------------------------------------------


@dataclass
class Alert:
    id: str
    when: str
    note: str = ""
    repeat: bool = False
    chat_id: int = 0
    thread_id: Optional[int] = None
    user_id: int = 0
    username: str = ""
    created_at: str = ""
    fired_at: Optional[str] = None  # the last time it fired
    fired_keys: List[str] = field(default_factory=list)  # with repeat: the windows it already fired in
    spent: bool = False  # a one-shot alert that fired

    @property
    def condition(self) -> Node:
        return parse(self.when)

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id, "when": self.when, "note": self.note, "repeat": self.repeat, "spent": self.spent,
            "fired_at": self.fired_at, "chat_id": self.chat_id, "thread_id": self.thread_id,
        }


class AlertStore:
    """One bot's alerts, in `state/bots/<id>/usage-alerts.json`. Each change holds a lock on the file:
    the bot's `alfred usage-alert` and the monitor in the pool both write it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_bot(cls, config: Any, bot_id: int) -> "AlertStore":
        return cls(config.bots_state_dir / str(int(bot_id)) / "usage-alerts.json")

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"alerts": [], "last_wake_at": None}
        return data if isinstance(data, dict) else {"alerts": [], "last_wake_at": None}

    @staticmethod
    def _alerts(data: Dict[str, Any]) -> List[Alert]:
        alerts = []
        for item in data.get("alerts") or []:
            try:
                alerts.append(Alert(**{key: value for key, value in item.items() if key in Alert.__dataclass_fields__}))
            except TypeError:
                continue
        return alerts

    def alerts(self) -> List[Alert]:
        return self._alerts(self._load())

    def last_wake_at(self) -> Optional[datetime]:
        value = self._load().get("last_wake_at")
        return parse_time(value) if value else None

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as handle:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def change(self, edit: Callable[[List[Alert], Dict[str, Any]], Any]) -> Any:
        """Read, edit and write under the lock. `edit` gets the alerts and the raw data, and may change both."""
        with self._locked():
            data = self._load()
            alerts = self._alerts(data)
            result = edit(alerts, data)
            data["alerts"] = [asdict(alert) for alert in alerts]
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(str(temporary), str(self.path))
            return result

    def add(self, when: str, *, note: str = "", repeat: bool = False, chat_id: int, thread_id: Optional[int] = None,
            user_id: int = 0, username: str = "") -> Alert:
        parse(when)  # refuse what can't be read before anything is written
        if len(note) > MAX_NOTE_LENGTH:
            raise ConditionError("the note is longer than %d characters" % MAX_NOTE_LENGTH)
        alert = Alert(id=uuid.uuid4().hex[:8], when=" ".join(when.split()), note=note.strip(), repeat=repeat,
                      chat_id=int(chat_id), thread_id=thread_id, user_id=int(user_id), username=username,
                      created_at=to_utc_text(utc_now()))

        def edit(alerts: List[Alert], data: Dict[str, Any]) -> Alert:
            if len([item for item in alerts if not item.spent]) >= MAX_ALERTS_PER_BOT:
                raise ConditionError("a bot can have at most %d alerts; remove one first" % MAX_ALERTS_PER_BOT)
            alerts.append(alert)
            return alert

        return self.change(edit)

    def remove(self, id_or_prefix: str) -> Optional[Alert]:
        prefix = id_or_prefix.strip().lower()

        def edit(alerts: List[Alert], data: Dict[str, Any]) -> Optional[Alert]:
            matches = [item for item in alerts if prefix and item.id.startswith(prefix)]
            if len(matches) != 1:
                return None
            alerts.remove(matches[0])
            return matches[0]

        return self.change(edit) if self.path.exists() else None


# -- deciding what fires ---------------------------------------------------------


@dataclass(frozen=True)
class Firing:
    alert: Alert
    key: str


def due(alerts: List[Alert], reading: Any, now: datetime) -> List[Firing]:
    """The alerts whose condition holds now and that have not fired in this window yet."""
    if reading is None or not reading.ok:
        return []
    values = values_of(reading, now)
    firings = []
    for alert in alerts:
        if alert.spent:
            continue
        try:
            node = alert.condition
        except ConditionError:
            continue  # written by an older version, or by hand: skip it rather than stop the others
        key = window_key(node, reading)
        if alert.repeat and key in alert.fired_keys:
            continue
        if node.check(values):
            firings.append(Firing(alert, key))
    return firings


def mark_fired(store: AlertStore, firings: List[Firing], now: datetime) -> List[Alert]:
    """Record the firings and the wake-up before the bot is woken. Returns the alerts as recorded."""
    wanted = {firing.alert.id: firing.key for firing in firings}

    def edit(alerts: List[Alert], data: Dict[str, Any]) -> List[Alert]:
        fired = []
        for alert in alerts:
            if alert.id not in wanted or alert.spent or (alert.repeat and wanted[alert.id] in alert.fired_keys):
                continue  # removed, or fired by someone else, since it was read
            alert.fired_at = to_utc_text(now)
            if alert.repeat:
                alert.fired_keys = (alert.fired_keys + [wanted[alert.id]])[-KEEP_FIRED_KEYS:]
            else:
                alert.spent = True
            fired.append(alert)
        if fired:
            data["last_wake_at"] = to_utc_text(now)
        return fired

    return store.change(edit)


def wake_allowed(store: AlertStore, now: datetime, interval_seconds: int) -> bool:
    last = store.last_wake_at()
    return last is None or (now - last).total_seconds() >= interval_seconds


# -- the event a bot gets ---------------------------------------------------------


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%a %d %b %H:%M %Z")


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return "%dd %dh" % (days, hours)
    return "%dh %02dm" % (hours, minutes) if hours else "%dm" % minutes


def describe_reading(reading: Any, now: datetime) -> str:
    lines = []
    for kind, label in (("session", "5-hour window"), ("weekly_all", "Week (all models)")):
        window = reading.window(kind)
        if window is None:
            lines.append("- %s: not running (nothing used since it last reset)" % label if kind == "session"
                         else "- %s: not reported" % label)
            continue
        reset = ("resets %s (in %s)" % (_local(window.resets_at), _span((window.resets_at - now).total_seconds()))
                 if window.resets_at else "reset time not reported")
        lines.append("- %s: %g%% used, %s" % (label, window.percent, reset))
    for window in reading.windows:
        if window.kind not in ("session", "weekly_all"):
            lines.append("- %s: %g%% used" % (window.label, window.percent))
    return "\n".join(lines)


def event_text(fired: List[Alert], reading: Any, now: datetime) -> str:
    lines = ["Usage alert%s met." % ("s" if len(fired) > 1 else "")]
    for alert in fired:
        lines.append("")
        lines.append("Alert %s: %s" % (alert.id, alert.when))
        if alert.note:
            lines.append("Your note when you set it: %s" % alert.note)
        lines.append("It repeats once per window." if alert.repeat
                     else "It will not fire again; it stays listed until removed.")
    lines += ["", "Plan usage read at %s:" % _local(reading.taken_at), describe_reading(reading, now), "",
              "Waking you used some plan usage. To stop an alert: alfred usage-alert remove <id>."]
    return "\n".join(lines)
