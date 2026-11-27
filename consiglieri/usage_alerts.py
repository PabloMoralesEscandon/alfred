"""Usage alerts for agents: "wake me when this is true about the Claude plan usage".

A bot registers an alert with `alfred usage-alert add --when "<condition>"`. The usage monitor, which
runs once per process in the bot pool, takes one reading per cycle and checks every bot's alerts against
it. When a condition holds, the bot that set the alert, and no other, is woken with an event that says
which condition held, what was read and when the windows reset.

Conditions compare these values, joined with `and`, `or`, `not` and parentheses (`and` binds tighter):

    session.used        percent of the 5-hour window used                 session.used < 10
    session.left        time until the 5-hour window resets               session.left < 1h
    week.used           percent of the weekly limit (all models)          week.used >= 80%
    week.left           time until the weekly limit resets                week.left <= 2d
    week.windows_left   useful 5-hour windows before the weekly reset     week.windows_left < 3
    week.max_spend      most of the week those windows can still spend    week.max_spend < 20
    week.surplus        weekly percent left minus week.max_spend:         week.surplus > 0
                        positive is quota that will be lost
    session.projected   percent the meter reaches at its reset at the     week.projected >= 100
    week.projected      recent pace (from the readings in the history)

The three forecast values (`usage_forecast.py`) depend on the active hours: the alert's own (--hours) or
CONSIGLIERI_USAGE_ACTIVE_HOURS, every hour by default. Operators: < <= > >= = == !=. Percentages are plain
numbers, with or without %. Times take a unit: s, m, h, d or w (90m, 1h, 2d). `reset` is accepted for
`left`, `windows` for `windows_left`, and `5h` for `session`. A value that was not read (no 5-hour
window running, a failed reading, no pace yet) makes its comparison false.

An alert fires once and is then spent, unless it was added with --repeat: then it fires at most once
per window (the windows its condition names), and again in the next one. An alert on the forecast
values belongs to the quota week: it fires once in a week, and again that week only if the surplus has
grown by CONSIGLIERI_USAGE_SURPLUS_STEP points (one window's share by default) since it last fired. Without
--repeat it is spent when that week ends; with it, it is armed again every week. What was fired is
written to disk before the bot is woken, so a restart does not fire it again.

Waking a bot costs plan usage (one agent turn with its whole context), unlike reading `/usage`.
So one bot is woken at most once per `CONSIGLIERI_USAGE_WAKE_INTERVAL`: alerts that hold sooner wait,
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

from . import usage_forecast
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
FIELDS = {"used": "used", "left": "left", "reset": "left", "windows_left": "windows_left", "windows": "windows_left",
          "max_spend": "max_spend", "surplus": "surplus", "projected": "projected"}
FORECAST_FIELDS = ("windows_left", "max_spend", "surplus")  # the weekly forecast; week only
PROJECTION_FIELDS = ("projected",)
VALUE_NAMES = ("session.used, session.left, session.projected, week.used, week.left, week.windows_left, "
               "week.max_spend, week.surplus or week.projected")
OPERATORS = ("<=", ">=", "==", "!=", "<", ">", "=")


class ConditionError(ValueError):
    pass


# -- conditions -------------------------------------------------------------

TOKEN = re.compile(r"\s*(?:(?P<paren>[()])|(?P<op><=|>=|==|!=|<|>|=)|(?P<word>-?[A-Za-z0-9_.%]+|&&|\|\|))")


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

    def uses(self, fields: Tuple[str, ...]) -> bool:
        return any(item.field in fields for item in self.comparisons())

    @property
    def uses_forecast(self) -> bool:
        return self.uses(FORECAST_FIELDS)

    def deciding(self, values: Dict[str, Optional[float]]) -> List[Comparison]:
        """The comparisons that decide the result: for a true `and`, all of them; for a false one, the first
        false part; for a true `or`, the parts that hold; for a false one, all of them."""
        if self.comparison is not None:
            return [self.comparison]
        if self.kind == "not":
            return self.children[0].deciding(values)
        result = self.check(values)
        if self.kind == "and":
            parts = self.children if result else [next(child for child in self.children if not child.check(values))]
        else:
            parts = [child for child in self.children if child.check(values)] if result else list(self.children)
        return [item for child in parts for item in child.deciding(values)]


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
            raise ConditionError("unknown value %r; use %s" % (name, VALUE_NAMES))
        field_name = FIELDS[field_name]
        if field_name in FORECAST_FIELDS and WINDOW_KINDS[window] != "weekly_all":
            raise ConditionError("%s is about the week: use week.%s" % (name, field_name))
        op = take()
        if op not in OPERATORS:
            raise ConditionError("expected a comparison (< <= > >= = !=) after %s, got %r" % (name, op))
        raw = take()
        if field_name in ("windows_left", "max_spend", "surplus", "projected"):
            try:
                value = float(raw.rstrip("%"))
            except ValueError:
                raise ConditionError("%s compares with a number, such as 0 or 8, not %r" % (name, raw)) from None
            if field_name == "windows_left" and raw.endswith("%"):
                raise ConditionError("%s counts windows, so it takes a plain number, not %r" % (name, raw))
        elif field_name == "used":
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


def values_of(reading: Any, now: datetime, forecast: Any = None,
              projected: Optional[Dict[str, Optional[float]]] = None) -> Dict[str, Optional[float]]:
    """What a condition can compare, from one reading: percent used and seconds left, per window, plus the
    weekly forecast (`usage_forecast.Forecast`) and the projected percents ({kind: percent}) when given."""
    values: Dict[str, Optional[float]] = {}
    for kind in ("session", "weekly_all"):
        window = reading.window(kind) if reading is not None and reading.ok else None
        values["%s.used" % kind] = window.percent if window is not None else None
        left = (window.resets_at - now).total_seconds() if window is not None and window.resets_at else None
        values["%s.left" % kind] = max(0.0, left) if left is not None else None
        values["%s.projected" % kind] = (projected or {}).get(kind)
    for name in FORECAST_FIELDS:
        values["weekly_all.%s" % name] = getattr(forecast, name) if forecast is not None else None
    return values


def _name(window: str) -> str:
    return "5-hour window" if window == "session" else "week"


def phrase(comparison: Comparison, values: Dict[str, Optional[float]]) -> str:
    """One comparison in words, with the value that was read: `week 90% used (resets in 3d 2h)`, and what it
    needed when it does not hold: `week 39% used, needs >= 90 (resets in 2d 19h)`."""
    actual = values.get("%s.%s" % (comparison.window, comparison.field))
    left = values.get("%s.left" % comparison.window)
    reset = " (resets in %s)" % _span(left) if left is not None else ""
    name = _name(comparison.window)
    field_name = comparison.field
    if actual is None:
        if comparison.window == "session" and values.get("session.used") is None:
            return "no 5-hour window running (nothing used since it last reset)"
        if values.get("weekly_all.used") is None:
            return "the week was not read"
        if field_name in FORECAST_FIELDS:
            return "no weekly forecast (the weekly reset time was not read)"
        if field_name == "projected":
            return "no pace for the %s yet (it needs readings at least 20 minutes apart)" % name
        return "%s was not read" % comparison.text.split()[0]
    if field_name == "used":
        text = "%s %g%% used" % (name, actual)
    elif field_name == "left":
        text, reset = "%s resets in %s" % (name, _span(actual)), ""
    elif field_name == "windows_left":
        text = "%g useful 5-hour windows left before the weekly reset" % actual
    elif field_name == "max_spend":
        text = "at most %g%% of the week can still be spent" % actual
    elif field_name == "surplus":
        if actual > 0:
            text = "%g%% of the week will go unused even using every remaining window fully" % actual
        elif actual == 0:
            text = "the remaining windows can spend exactly what is left of the week, nothing to spare"
        else:
            text = "the remaining windows can spend %g%% more than is left of the week" % -actual
    else:
        text = "at the recent pace the %s reaches %g%% by its reset" % (name, actual)
    if not comparison.check(values):
        text += ", needs %s" % " ".join(comparison.text.split()[1:])
    return text + reset


def verdict(node: Node, values: Dict[str, Optional[float]]) -> Tuple[bool, str]:
    """True or false, and a short reason in one line made of the comparisons that decided it."""
    result = node.check(values)
    phrases: List[str] = []
    for comparison in node.deciding(values):
        text = phrase(comparison, values)
        if text not in phrases:
            phrases.append(text)
    return result, "; ".join(phrases)


def window_key(node: Node, reading: Any) -> str:
    """Which windows a firing belongs to: the reset times of the windows the condition names.
    An alert on the forecast belongs to the quota week alone."""
    parts = []
    for kind in (["weekly_all"] if node.uses_forecast else node.windows()):
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
    hours: str = ""  # active hours for the forecast values (usage_forecast.parse_hours); "" = the default
    last_surplus: Optional[float] = None  # forecast alerts: week.surplus when it last fired
    expires_at: Optional[str] = None  # one-shot forecast alerts: spent once this weekly reset has passed

    @property
    def condition(self) -> Node:
        return parse(self.when)

    def is_spent(self, now: Optional[datetime] = None) -> bool:
        if self.spent:
            return True
        return bool(self.expires_at) and (now or utc_now()) >= parse_time(self.expires_at)

    def describe(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        data = {
            "id": self.id, "when": self.when, "note": self.note, "repeat": self.repeat, "spent": self.is_spent(now),
            "fired_at": self.fired_at, "chat_id": self.chat_id, "thread_id": self.thread_id,
        }
        if self.hours:
            data["hours"] = self.hours
        return data


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
            user_id: int = 0, username: str = "", hours: str = "") -> Alert:
        node = parse(when)  # refuse what can't be read before anything is written
        if len(note) > MAX_NOTE_LENGTH:
            raise ConditionError("the note is longer than %d characters" % MAX_NOTE_LENGTH)
        if hours:
            if not node.uses_forecast:
                raise ConditionError("active hours only change week.windows_left, week.max_spend and week.surplus, "
                                     "and the condition uses none of them")
            try:
                hours = usage_forecast.parse_hours(hours).spec()
            except usage_forecast.HoursError as exc:
                raise ConditionError(str(exc)) from None
        alert = Alert(id=uuid.uuid4().hex[:8], when=" ".join(when.split()), note=note.strip(), repeat=repeat,
                      chat_id=int(chat_id), thread_id=thread_id, user_id=int(user_id), username=username,
                      created_at=to_utc_text(utc_now()), hours=hours)

        def edit(alerts: List[Alert], data: Dict[str, Any]) -> Alert:
            if len([item for item in alerts if not item.is_spent()]) >= MAX_ALERTS_PER_BOT:
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


# What a condition sees for one alert: its values, and the weekly forecast they came from (or None).
ValuesFor = Callable[[Alert, Node], Tuple[Dict[str, Optional[float]], Any]]


@dataclass(frozen=True)
class Firing:
    alert: Alert
    key: str
    forecast: Any = None  # usage_forecast.Forecast, for alerts on the forecast values


def due(alerts: List[Alert], reading: Any, now: datetime, values_for: Optional[ValuesFor] = None,
        surplus_step: float = 8.0) -> List[Firing]:
    """The alerts whose condition holds now and that have not fired in this window yet.

    `values_for` gives each alert its values (the forecast depends on its active hours); without it only
    the plain values are known. An alert on the forecast fires once per quota week, and again in the same
    week only when week.surplus has grown by `surplus_step` points since it last fired.
    """
    if reading is None or not reading.ok:
        return []
    plain = values_of(reading, now)
    firings = []
    for alert in alerts:
        if alert.is_spent(now):
            continue
        try:
            node = alert.condition
        except ConditionError:
            continue  # written by an older version, or by hand: skip it rather than stop the others
        values, forecast = values_for(alert, node) if values_for is not None else (plain, None)
        key = window_key(node, reading)
        if node.uses_forecast:
            if not node.check(values):
                continue
            if key in alert.fired_keys:
                surplus = values.get("weekly_all.surplus")
                if alert.last_surplus is None or surplus is None or surplus < alert.last_surplus + surplus_step:
                    continue
            firings.append(Firing(alert, key, forecast))
            continue
        if alert.repeat and key in alert.fired_keys:
            continue
        if node.check(values):
            firings.append(Firing(alert, key, forecast))
    return firings


def mark_fired(store: AlertStore, firings: List[Firing], now: datetime) -> List[Alert]:
    """Record the firings and the wake-up before the bot is woken. Returns the alerts as recorded."""
    wanted = {firing.alert.id: firing for firing in firings}

    def edit(alerts: List[Alert], data: Dict[str, Any]) -> List[Alert]:
        fired = []
        for alert in alerts:
            firing = wanted.get(alert.id)
            if firing is None or alert.is_spent(now) or alert.fired_at != firing.alert.fired_at:
                continue  # removed, or fired by someone else, since it was read
            if firing.forecast is None and alert.repeat and firing.key in alert.fired_keys:
                continue
            alert.fired_at = to_utc_text(now)
            if firing.forecast is not None:
                if firing.key not in alert.fired_keys:
                    alert.fired_keys = (alert.fired_keys + [firing.key])[-KEEP_FIRED_KEYS:]
                alert.last_surplus = firing.forecast.surplus
                if not alert.repeat:
                    alert.expires_at = to_utc_text(firing.forecast.week_resets_at)
            elif alert.repeat:
                alert.fired_keys = (alert.fired_keys + [firing.key])[-KEEP_FIRED_KEYS:]
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


def forecast_lines(forecast: Any, now: datetime) -> List[str]:
    """What a bot is told about quota that can't be used: what is left, the useful windows, the most they can
    spend, what will be lost and when the week resets."""
    reset = "%s (in %s)" % (_local(forecast.week_resets_at), _span((forecast.week_resets_at - now).total_seconds()))
    lost = forecast.surplus
    if lost > 0:
        verdict_text = "%g%% of this week's quota will go unused even if every remaining window is used fully." % lost
    elif lost == 0:
        verdict_text = "Nothing to spare: only using every remaining window fully avoids losing weekly quota."
    else:
        verdict_text = "The remaining windows can still spend all of it, with %g%% to spare." % -lost
    return [
        "Verdict: %s" % verdict_text,
        "- Left this week: %g%%, resets %s" % (forecast.week_left, reset),
        "- Useful 5-hour windows left: %g (active hours: %s)" % (forecast.windows_left, forecast.hours.describe()),
        "- Most that can still be spent: %g%% (%g windows x %g%% of the week each, %s)"
        % (forecast.max_spend, forecast.windows_left, forecast.share, forecast.share_source),
        "- Will be lost: %g%%" % max(0.0, lost),
    ]


def event_text(fired: List[Alert], reading: Any, now: datetime, forecasts: Optional[Dict[str, Any]] = None,
               surplus_step: float = 8.0) -> str:
    """The event a bot is woken with. `forecasts` maps alert ids to the weekly forecast they fired on."""
    forecasts = forecasts or {}
    lines = ["Usage alert%s met." % ("s" if len(fired) > 1 else "")]
    for alert in fired:
        lines.append("")
        lines.append("Alert %s: %s" % (alert.id, alert.when))
        if alert.note:
            lines.append("Your note when you set it: %s" % alert.note)
        forecast = forecasts.get(alert.id)
        if forecast is not None:
            lines += forecast_lines(forecast, now)
            again = "again this week only if the loss grows by %g points or more" % surplus_step
            lines.append("It fires once per quota week, %s; %s." % (
                again, "it is armed again every week" if alert.repeat else "it is spent when this week resets"))
        else:
            lines.append("It repeats once per window." if alert.repeat
                         else "It will not fire again; it stays listed until removed.")
    lines += ["", "Plan usage read at %s:" % _local(reading.taken_at), describe_reading(reading, now), "",
              "This is the monitor's verdict: act on it; there is no need to read or recompute the usage yourself.",
              "Waking you used some plan usage. To stop an alert: alfred usage-alert remove <id>."]
    return "\n".join(lines)
