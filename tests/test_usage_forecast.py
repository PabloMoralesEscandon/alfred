"""The weekly quota forecast: useful 5-hour windows, the most that can be spent, the surplus that will be lost,
active hours, the window in progress, the weekly reset in the middle, and what the history measures."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

from alfred import usage_forecast as uf
from alfred.usage_forecast import HoursError, estimate_share, forecast, parse_hours, projected
from alfred.usage_monitor import Reading, Window

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)  # a Thursday


def reading(week_used: float = 52.0, week_left: timedelta = timedelta(hours=30), session: Optional[float] = None,
            session_left: Optional[timedelta] = None, at: datetime = NOW) -> Reading:
    windows = []
    if session is not None:
        windows.append(Window("session", "Session", session, resets_at=at + session_left if session_left else None))
    windows.append(Window("weekly_all", "Week (all models)", week_used, resets_at=at + week_left))
    return Reading(taken_at=at, source="usage_report", windows=tuple(windows))


class ForecastTests(unittest.TestCase):
    def test_the_users_example_six_windows_and_48_percent_is_the_exact_limit(self) -> None:
        result = forecast(reading(week_used=52, week_left=timedelta(hours=30)), NOW, 8.0)
        self.assertEqual((result.windows_left, result.max_spend, result.week_left, result.surplus), (6.0, 48.0, 48.0, 0.0))
        more = forecast(reading(week_used=50, week_left=timedelta(hours=30)), NOW, 8.0)
        self.assertEqual(more.surplus, 2.0)  # 50% left, 48% can be spent: 2% is lost
        less = forecast(reading(week_used=52, week_left=timedelta(hours=40)), NOW, 8.0)
        self.assertEqual((less.windows_left, less.surplus), (8.0, -16.0))  # room to spare

    def test_active_hours_9_to_20_against_all_day(self) -> None:
        current = reading(week_used=52, week_left=timedelta(hours=30))  # resets Friday 16:00 UTC
        all_day = forecast(current, NOW, 8.0)
        office = forecast(current, NOW, 8.0, parse_hours("9-20 UTC"))
        # Thursday 10:00-20:00 and Friday 09:00-16:00: 17 active hours, so 3.4 windows
        self.assertEqual((office.windows_left, office.max_spend, office.surplus), (3.4, 27.2, 20.8))
        self.assertEqual([(w.start.hour, round(w.weight, 2)) for w in office.windows],
                         [(10, 1.0), (15, 1.0), (9, 1.0), (14, 0.4)])
        self.assertGreater(office.surplus, all_day.surplus)

    def test_days_of_the_week_and_a_range_across_midnight(self) -> None:
        current = reading(week_used=0, week_left=timedelta(days=7))  # Thursday 10:00 to the next Thursday 10:00
        weekdays = forecast(current, NOW, 8.0, parse_hours("9-20 mon-fri UTC"))
        # Thu 10-20 (10 h), Fri, Mon, Tue, Wed (11 h each), next Thu 9-10 (1 h): 55 h
        self.assertEqual(weekdays.windows_left, 11.0)
        night = forecast(reading(week_used=0, week_left=timedelta(hours=24)), NOW, 8.0, parse_hours("22-2 UTC"))
        self.assertEqual(night.windows_left, 0.8)  # Thursday 22:00 to Friday 02:00, 4 h

    def test_the_window_in_progress_counts_by_what_is_left_of_it(self) -> None:
        # 40% used, 4 h left: 0.6 of a window, then 26 h of new windows (5.2)
        current = forecast(reading(session=40, session_left=timedelta(hours=4)), NOW, 8.0)
        self.assertEqual(current.current.weight, 0.6)
        self.assertEqual(current.windows_left, 5.8)
        # 40% used but only 2 h left: no more than 2/5 of a window fits, under prorata
        short = forecast(reading(session=40, session_left=timedelta(hours=2)), NOW, 8.0)
        self.assertEqual((short.current.weight, short.windows_left), (0.4, 6.0))
        # a window used up still delays the next one
        spent = forecast(reading(session=100, session_left=timedelta(hours=3)), NOW, 8.0)
        self.assertEqual((spent.current.weight, spent.windows_left), (0.0, 5.4))
        # outside the active hours the window in progress is worth nothing
        late = NOW.replace(hour=21)
        evening = forecast(reading(session=0, session_left=timedelta(hours=3), at=late), late, 8.0, parse_hours("9-20 UTC"))
        self.assertEqual(evening.current.weight, 0.0)

    def test_the_weekly_reset_cuts_the_last_window(self) -> None:
        current = reading(week_used=40, week_left=timedelta(hours=32))
        prorata = forecast(current, NOW, 8.0)
        self.assertEqual(prorata.windows_left, 6.4)  # six windows and 2 of the 5 hours of the seventh
        full = forecast(current, NOW, 8.0, partial="full")
        self.assertEqual(full.windows_left, 7.0)  # the looser bound: it starts before the reset, so it counts
        self.assertTrue(all(window.start < current.window("weekly_all").resets_at for window in full.windows))
        # a 5-hour window that resets after the week does: only its part before the weekly reset counts
        crossing = forecast(reading(week_used=95, week_left=timedelta(hours=1), session=0,
                                    session_left=timedelta(hours=4)), NOW, 8.0)
        self.assertEqual((crossing.windows_left, crossing.max_spend, crossing.surplus), (0.2, 1.6, 3.4))

    def test_full_mode_counts_late_windows_whole(self) -> None:
        current = reading(week_used=52, week_left=timedelta(hours=30))
        full = forecast(current, NOW, 8.0, parse_hours("9-20 UTC"), partial="full")
        self.assertEqual(full.windows_left, 4.0)  # 10:00, 15:00, Friday 09:00 and 14:00

    def test_no_forecast_without_a_weekly_reset_ahead(self) -> None:
        self.assertIsNone(forecast(Reading.failed("rate limited", "text", NOW), NOW, 8.0))
        text_only = Reading(NOW, "text", (Window("weekly_all", "Week", 40.0),))
        self.assertIsNone(forecast(text_only, NOW, 8.0))
        self.assertIsNone(forecast(reading(week_left=timedelta(hours=1)), NOW + timedelta(hours=2), 8.0))

    def test_the_forecast_explains_itself(self) -> None:
        result = forecast(reading(), NOW, 8.0, parse_hours("9-20 mon-fri UTC"), share_source="default")
        data = result.to_dict()
        for key in ("windows_left", "max_spend", "surplus", "week_left", "week_resets_at", "windows", "assumptions"):
            self.assertIn(key, data)
        text = " ".join(result.assumptions())
        for expected in ("first message", "5 hours", "8% of the weekly limit (default)", "prorata",
                         "09:00-20:00 Mon-Fri (UTC)"):
            self.assertIn(expected, text)


class ActiveHoursTests(unittest.TestCase):
    def test_forms_it_reads(self) -> None:
        for text, spec in (
            ("9-20", "09:00-20:00"), ("09:30-18:00 mon-fri", "09:30-18:00 mon-fri"),
            ("22-2 weekdays Europe/Madrid", "22:00-02:00 mon-fri Europe/Madrid"), ("always", "always"),
            ("", "always"), ("0-24", "always"), ("9-20 sat,sun", "09:00-20:00 sat,sun"),
            ("8-14 mon,wed,fri UTC", "08:00-14:00 mon,wed,fri UTC"), ("9-17 fri-mon", "09:00-17:00 mon,fri-sun"),
        ):
            self.assertEqual(parse_hours(text).spec(), spec, text)
        self.assertEqual(parse_hours("9-20", days="mon-fri", tz="UTC").spec(), "09:00-20:00 mon-fri UTC")
        self.assertEqual(parse_hours(parse_hours("7-23 weekends Asia/Tokyo").spec()).spec(), "07:00-23:00 sat,sun Asia/Tokyo")

    def test_bad_forms_are_refused(self) -> None:
        for text in ("25-3", "9-9", "9:75-10", "noon-5", "mon-xyz", "9-20 Mars/Olympus"):
            with self.assertRaises(HoursError, msg=text):
                parse_hours(text)

    def test_daylight_saving_keeps_the_wall_clock(self) -> None:
        # Europe/Madrid leaves summer time on 25 October 2026: 9:00 local is 07:00 UTC before, 08:00 UTC after
        hours = parse_hours("9-10 Europe/Madrid")
        before = hours.periods(datetime(2026, 10, 24, tzinfo=timezone.utc), datetime(2026, 10, 25, tzinfo=timezone.utc))
        after = hours.periods(datetime(2026, 10, 26, tzinfo=timezone.utc), datetime(2026, 10, 27, tzinfo=timezone.utc))
        self.assertEqual((before[0][0].hour, after[0][0].hour), (7, 8))


class HistoryTests(unittest.TestCase):
    def test_measures_the_share_of_a_full_window(self) -> None:
        week_reset = NOW + timedelta(days=3)
        history = []
        for window in range(2):  # two 5-hour windows, each used from 0 to 60%, while the week rises 5 points each
            start = NOW + timedelta(hours=5 * window)
            for step in range(4):
                history.append(Reading(start + timedelta(hours=step), "usage_report", (
                    Window("session", "Session", 20.0 * step, resets_at=start + timedelta(hours=5)),
                    Window("weekly_all", "Week", 30 + 5 * window + 5 * step / 3, resets_at=week_reset))))
        estimate = estimate_share(history)
        self.assertEqual((estimate.share, estimate.session_points, estimate.segments), (8.3, 120.0, 2))
        self.assertIsNone(estimate_share(history[:4]))  # 60 points of one window: not enough yet

    def test_projects_the_pace_to_the_reset(self) -> None:
        older = reading(week_used=60, session=20, session_left=timedelta(hours=3), at=NOW - timedelta(minutes=30))
        newer = reading(week_used=62, session=50, session_left=timedelta(hours=2, minutes=30))
        # 30 points in 30 minutes, 2.5 hours to go: 50 + 150
        self.assertEqual(projected([older, newer], newer, NOW, "session", timedelta(hours=1)), 200.0)
        self.assertIsNone(projected([newer], newer, NOW, "session", timedelta(hours=1)))  # no pace yet
        too_close = reading(session=45, session_left=timedelta(hours=2, minutes=40), at=NOW - timedelta(minutes=10))
        self.assertIsNone(projected([too_close, newer], newer, NOW, "session", timedelta(hours=1)))


if __name__ == "__main__":
    unittest.main()
