"""Weekly quota forecast: how much of the weekly limit can still be spent before it resets.

Pure functions with no input or output, so they can be tested with a fixed clock: they take a reading
(anything with `window(kind)` returning objects with `percent` and `resets_at`, such as
`usage_monitor.Reading`), the current time and the user's active hours.

The question they answer: with the 5-hour windows that still fit before the weekly reset, inside the
hours the user actually works, can the rest of the weekly limit be spent? If not, the difference is
quota that will be lost (`surplus`).

Assumptions, also listed by `Forecast.assumptions()` and in docs/USAGE_MONITOR_RESEARCH.md, section 13:

1. A 5-hour window starts with the first message and lasts 5 hours. The next one can start when it ends.
2. The user starts each window as early as possible: at the first active moment after the previous one
   ends. That gives the most that could be spent, so the forecast is an upper bound and a positive
   surplus means quota is lost even in the best case.
3. One fully used window takes `share` percent of the weekly limit (ALFRED_USAGE_WINDOW_SHARE, 8 by
   default, or the share measured from the history when there is enough of it).
4. Partial windows (`partial`):
   - `prorata` (default): a window counts by the part of its 5 hours that falls inside the active hours
     and before the weekly reset. At most one full window per 5 active hours: a window started at 19:00
     with active hours until 20:00 counts 0.2.
   - `full`: every window that starts inside the active hours before the weekly reset counts whole, as if
     a window could be used up in less than 5 hours. A looser bound.
5. The window in progress counts by what is left of it (100 - session.used, as a fraction) and, under
   `prorata`, by no more than its remaining active time divided by 5 hours.
6. Nothing spent after the weekly reset counts for this week: windows are cut at the reset.
7. Active hours are a daily range in a time zone (the machine's by default), on some weekdays. A range
   that crosses midnight (22-2) belongs to the day it starts on. Without active hours, every hour counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

WINDOW = timedelta(hours=5)
WINDOW_SECONDS = WINDOW.total_seconds()
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EVERY_DAY = tuple(range(7))
PARTIAL_MODES = ("prorata", "full")
RESET_ROUNDING = 300  # seconds: the server's reset times wobble by a fraction of a second

_RANGE = re.compile(r"^(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?$")
_DAY_WORDS = {"weekdays": (0, 1, 2, 3, 4), "weekends": (5, 6), "daily": EVERY_DAY, "everyday": EVERY_DAY}


class HoursError(ValueError):
    pass


# -- active hours --------------------------------------------------------------


@dataclass(frozen=True)
class ActiveHours:
    """The hours the user uses Claude: a daily range in a time zone, on some weekdays."""

    start: int = 0  # minutes after local midnight
    end: int = 24 * 60  # minutes after local midnight; at or before `start` means it ends the next day
    days: Tuple[int, ...] = EVERY_DAY  # weekdays (0 = Monday) a range starts on
    tz: Optional[str] = None  # an IANA name such as Europe/Madrid; None is this machine's local time

    @property
    def always(self) -> bool:
        return self.start == 0 and self.end == 24 * 60 and tuple(sorted(self.days)) == EVERY_DAY

    def _zone(self) -> Any:
        if not self.tz:
            return None
        from zoneinfo import ZoneInfo  # Python 3.9+

        return ZoneInfo(self.tz)

    def spec(self) -> str:
        """The normalized text form, which `parse_hours` reads back: `09:00-20:00 mon-fri Europe/Madrid`."""
        if self.always:
            return "always"
        parts = ["%s-%s" % (_clock(self.start), _clock(self.end))]
        if tuple(sorted(self.days)) != EVERY_DAY:
            parts.append(_days_text(self.days))
        if self.tz:
            parts.append(self.tz)
        return " ".join(parts)

    def describe(self) -> str:
        if self.always:
            return "all day, every day"
        days = "every day" if tuple(sorted(self.days)) == EVERY_DAY else _days_text(self.days).title()
        return "%s-%s %s (%s)" % (_clock(self.start), _clock(self.end), days, self.tz or "local time")

    def periods(self, start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
        """The active stretches between `start` and `end` (aware), in UTC, sorted and merged."""
        if start >= end:
            return []
        if self.always:
            return [(start, end)]
        zone = self._zone()

        def local_date(moment: datetime) -> date:
            return (moment.astimezone(zone) if zone else moment.astimezone()).date()

        def at(day: date, minutes: int) -> datetime:
            naive = datetime.combine(day, time()) + timedelta(minutes=minutes)
            # A naive datetime is read as local time, with this machine's daylight saving rules.
            return (naive.replace(tzinfo=zone) if zone else naive).astimezone(timezone.utc)

        found: List[Tuple[datetime, datetime]] = []
        day = local_date(start) - timedelta(days=1)  # a range from yesterday may still be running
        last = local_date(end)
        while day <= last:
            if day.weekday() in self.days:
                begins = at(day, self.start)
                ends = at(day, self.end if self.end > self.start else self.end + 24 * 60)
                begins, ends = max(begins, start), min(ends, end)
                if begins < ends:
                    found.append((begins, ends))
            day += timedelta(days=1)
        merged: List[Tuple[datetime, datetime]] = []
        for begins, ends in sorted(found):
            if merged and begins <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], ends))
            else:
                merged.append((begins, ends))
        return merged


ALWAYS = ActiveHours()


def _clock(minutes: int) -> str:
    return "%02d:%02d" % divmod(minutes, 60)


def _days_text(days: Iterable[int]) -> str:
    """(0, 1, 2, 3, 4) -> mon-fri; (0, 2, 4) -> mon,wed,fri; runs of three or more become ranges."""
    ordered = sorted(set(days))
    runs: List[List[int]] = []
    for day in ordered:
        if runs and day == runs[-1][-1] + 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    parts = []
    for run in runs:
        if len(run) >= 3:
            parts.append("%s-%s" % (DAY_NAMES[run[0]], DAY_NAMES[run[-1]]))
        else:
            parts.extend(DAY_NAMES[day] for day in run)
    return ",".join(parts)


def _parse_days(text: str) -> Tuple[int, ...]:
    text = text.lower()
    if text in _DAY_WORDS:
        return _DAY_WORDS[text]
    days = set()
    for part in text.split(","):
        first, _, last = part.partition("-")
        if first not in DAY_NAMES or (last and last not in DAY_NAMES):
            raise HoursError("unknown days %r; use mon-fri, sat,sun, weekdays or weekends" % text)
        a, b = DAY_NAMES.index(first), DAY_NAMES.index(last or first)
        days.update(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
    return tuple(sorted(days))


def _check_zone(name: str) -> str:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
    except Exception:
        raise HoursError("unknown time zone %r; use a name such as Europe/Madrid or UTC" % name) from None
    return name


def parse_hours(text: Optional[str], days: Optional[str] = None, tz: Optional[str] = None) -> ActiveHours:
    """`9-20`, `09:00-20:30 mon-fri`, `22-2 weekdays Europe/Madrid`, `always`. Raises HoursError.

    `days` and `tz` add to (or replace) what the text says, for the separate --days and --tz options.
    An empty text means every hour of every day.
    """
    start, end, chosen_days, zone = 0, 24 * 60, EVERY_DAY, None
    for token in (text or "").split():
        lowered = token.lower()
        match = _RANGE.match(lowered)
        if match:
            start_hour, start_minute, end_hour, end_minute = (int(group or 0) for group in match.groups())
            if start_hour > 23 or end_hour > 24 or start_minute > 59 or end_minute > 59 or (end_hour == 24 and end_minute):
                raise HoursError("hours go from 0 to 24, such as 9-20 or 08:30-18:00, not %r" % token)
            start, end = start_hour * 60 + start_minute, end_hour * 60 + end_minute
            if start == end % (24 * 60) and not (start == 0 and end == 24 * 60):
                raise HoursError("the range %r is empty; for every hour use 0-24 or always" % token)
        elif lowered in ("always", "24h", "all", "any"):
            start, end = 0, 24 * 60
        elif "/" in token or token.upper() in ("UTC", "GMT"):
            zone = _check_zone(token)
        elif lowered in _DAY_WORDS or re.match(r"^[a-z]{3}(-[a-z]{3})?(,[a-z]{3}(-[a-z]{3})?)*$", lowered):
            chosen_days = _parse_days(lowered)
        else:
            raise HoursError("can't read %r in the active hours; use a range such as 9-20, days such as mon-fri "
                             "and a time zone such as Europe/Madrid" % token)
    if days:
        chosen_days = _parse_days(days.strip())
    if tz:
        zone = _check_zone(tz.strip())
    return ActiveHours(start, end, chosen_days, zone)


def overlap(periods: Sequence[Tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    """Seconds of the periods that fall between `start` and `end`."""
    return sum(max(0.0, (min(end, b) - max(start, a)).total_seconds()) for a, b in periods)


def next_active(periods: Sequence[Tuple[datetime, datetime]], moment: datetime) -> Optional[datetime]:
    for a, b in periods:
        if b > moment:
            return max(a, moment)
    return None


# -- the forecast -------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedWindow:
    """One 5-hour window the forecast counts: when it would run and how much of a full window it is worth."""

    start: datetime
    end: datetime
    weight: float  # 0 to 1: the fraction of a fully used window it can still take
    current: bool = False  # the window already running

    def to_dict(self) -> Dict[str, Any]:
        return {"start": _iso(self.start), "end": _iso(self.end), "weight": round(self.weight, 2), "current": self.current}


@dataclass(frozen=True)
class Forecast:
    now: datetime
    week_used: float
    week_resets_at: datetime
    share: float
    hours: ActiveHours = ALWAYS
    partial: str = "prorata"
    share_source: str = "configured"
    windows: Tuple[PlannedWindow, ...] = field(default_factory=tuple)

    @property
    def week_left(self) -> float:
        return round(max(0.0, 100.0 - self.week_used), 1)

    @property
    def windows_left(self) -> float:
        return round(sum(window.weight for window in self.windows), 2)

    @property
    def max_spend(self) -> float:
        return round(self.windows_left * self.share, 1)

    @property
    def surplus(self) -> float:
        """Percent of the weekly limit that would be left at the reset even using every useful window fully.
        Positive: quota that will be lost. Zero: nothing is lost only if every window is used to the full."""
        return round(self.week_left - self.max_spend, 1)

    @property
    def current(self) -> Optional[PlannedWindow]:
        return next((window for window in self.windows if window.current), None)

    def assumptions(self) -> List[str]:
        partial = (
            "a window counts by the part of its 5 hours inside the active hours and before the weekly reset "
            "(at most one full window per 5 active hours)" if self.partial == "prorata" else
            "every window that starts inside the active hours before the weekly reset counts whole"
        )
        return [
            "A 5-hour window starts with the first message and lasts 5 hours; the next starts at the first active "
            "moment after it ends (as early as possible, so this is the most that can be spent).",
            "One fully used window takes %g%% of the weekly limit (%s)." % (self.share, self.share_source),
            "Partial windows (%s): %s." % (self.partial, partial),
            "The window in progress counts by what is left of it, and only its time before the weekly reset.",
            "Active hours: %s." % self.hours.describe(),
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "week_used": self.week_used, "week_left": self.week_left, "week_resets_at": _iso(self.week_resets_at),
            "windows_left": self.windows_left, "max_spend": self.max_spend, "surplus": self.surplus,
            "window_share": self.share, "window_share_source": self.share_source, "partial": self.partial,
            "active_hours": self.hours.spec(), "active_hours_text": self.hours.describe(),
            "windows": [window.to_dict() for window in self.windows], "assumptions": self.assumptions(),
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def forecast(reading: Any, now: datetime, share: float, hours: Optional[ActiveHours] = None,
             partial: str = "prorata", share_source: str = "configured") -> Optional[Forecast]:
    """The windows left before the weekly reset and what they can spend. None without a weekly reset time
    in the future (a failed reading, the text fallback, or a reading older than the reset)."""
    hours = hours or ALWAYS
    if partial not in PARTIAL_MODES:
        raise ValueError("partial must be one of %s" % ", ".join(PARTIAL_MODES))
    if reading is None or not getattr(reading, "ok", True) or share <= 0:
        return None
    week = reading.window("weekly_all")
    if week is None or week.resets_at is None or week.resets_at <= now:
        return None
    reset = week.resets_at
    periods = hours.periods(now, reset)
    windows: List[PlannedWindow] = []
    cursor = now
    session = reading.window("session")
    if session is not None and session.resets_at is not None and session.resets_at > now:
        ends = min(session.resets_at, reset)
        active = overlap(periods, now, ends)
        unused = max(0.0, 1.0 - session.percent / 100.0)
        weight = min(unused, active / WINDOW_SECONDS) if partial == "prorata" else (unused if active > 0 else 0.0)
        windows.append(PlannedWindow(now, session.resets_at, weight, current=True))
        cursor = session.resets_at
    for _ in range(64):  # a week holds at most 34 windows; the bound only guards against odd readings
        begins = next_active(periods, cursor)
        if begins is None or begins >= reset:
            break
        ends = begins + WINDOW
        weight = overlap(periods, begins, min(ends, reset)) / WINDOW_SECONDS if partial == "prorata" else 1.0
        windows.append(PlannedWindow(begins, ends, weight))
        cursor = ends
    return Forecast(now=now, week_used=week.percent, week_resets_at=reset, share=share, hours=hours,
                    partial=partial, share_source=share_source, windows=tuple(windows))


# -- what the history tells -----------------------------------------------------------


def _key(window: Any) -> Optional[int]:
    if window is None or window.resets_at is None:
        return None
    return int(round(window.resets_at.timestamp() / RESET_ROUNDING))


@dataclass(frozen=True)
class ShareEstimate:
    """The weekly share of one full window, measured: the weekly rise divided by the 5-hour rise."""

    share: float
    session_points: float  # how many points of 5-hour windows the measure covers (100 = one full window)
    week_points: float
    segments: int

    def to_dict(self) -> Dict[str, Any]:
        return {"share": self.share, "session_points": self.session_points, "week_points": self.week_points,
                "segments": self.segments}


def estimate_share(readings: Sequence[Any], min_session_points: float = 100.0) -> Optional[ShareEstimate]:
    """From readings oldest first: within each stretch where both the 5-hour window and the week stay the same
    (same reset times), compare how much each rose. None until the stretches cover `min_session_points`
    points of 5-hour windows, because the server rounds both meters to whole percents."""
    segments: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    for reading in readings:
        if not getattr(reading, "ok", False):
            continue
        session, week = reading.window("session"), reading.window("weekly_all")
        session_key, week_key = _key(session), _key(week)
        if session_key is None or week_key is None:
            continue
        segments.setdefault((session_key, week_key), []).append((session.percent, week.percent))
    session_points = week_points = 0.0
    used = 0
    for points in segments.values():
        rise = points[-1][0] - points[0][0]
        week_rise = points[-1][1] - points[0][1]
        if rise > 0 and week_rise >= 0:
            session_points += rise
            week_points += week_rise
            used += 1
    if session_points < min_session_points:
        return None
    share = round(100.0 * week_points / session_points, 1)
    if not 0.5 <= share <= 100:
        return None
    return ShareEstimate(share, session_points, week_points, used)


def projected(readings: Sequence[Any], reading: Any, now: datetime, kind: str,
              lookback: timedelta, min_span: timedelta = timedelta(minutes=20)) -> Optional[float]:
    """The percent the meter `kind` would reach at its reset at the pace of the last `lookback`.

    Needs an earlier reading of the same window (same reset time) at least `min_span` before the newest.
    None without one, or without a reset time. Never less than the current percent.
    """
    if reading is None or not getattr(reading, "ok", False):
        return None
    window = reading.window(kind)
    if window is None or window.resets_at is None or window.resets_at <= now:
        return None
    key = _key(window)
    earlier = [item for item in readings if getattr(item, "ok", False) and item.window(kind) is not None
               and _key(item.window(kind)) == key and reading.taken_at - lookback <= item.taken_at
               and reading.taken_at - item.taken_at >= min_span]
    if not earlier:
        return None
    oldest = min(earlier, key=lambda item: item.taken_at)
    span = (reading.taken_at - oldest.taken_at).total_seconds()
    pace = max(0.0, (window.percent - oldest.window(kind).percent) / span)
    return round(window.percent + pace * (window.resets_at - now).total_seconds(), 1)
