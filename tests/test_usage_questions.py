"""Questions an agent asks about the plan usage, and alerts on the weekly forecast.

- `alfred usage check|running_low|forecast`: a one-line verdict by default, the breakdown with --json,
  exit codes, the reading cache and its cap, the usage permission, and no model call.
- `alfred usage-alert add 'week.surplus > 0' --hours ...` and `usage-alert waste`: one firing per quota
  week, again only when the loss grows, active hours, the margin, and the event the bot gets.
- The base skill tells agents to act on the verdict.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from alfred import bots, cli, extensions, home, usage_alerts
from alfred.bots import BotRecord, BotRegistry
from alfred.scheduler import KIND_EVENT, SchedulerStore
from alfred.usage_alerts import AlertStore, ConditionError, parse
from alfred.usage_monitor import (Evaluation, MonitorSettings, Reading, UsageHistory, UsageMonitor, Window,
                                  read_on_demand)
from fakes import Sandbox
from test_usage_alerts import FixedSource

BOT = 5000000001
OTHER = 5000000002
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)  # a Thursday


def limits(session: Optional[float], session_left: Optional[timedelta], week: float, week_left: timedelta,
           now: datetime) -> Dict[str, Any]:
    rows = []
    if session is not None:
        rows.append({"kind": "session", "group": "session", "percent": session, "severity": "normal",
                     "resets_at": (now + session_left).isoformat() if session_left else None})
    rows.append({"kind": "weekly_all", "group": "weekly", "percent": week, "severity": "normal",
                 "resets_at": (now + week_left).isoformat()})
    rows.append({"kind": "weekly_scoped", "group": "weekly", "percent": 59, "severity": "normal",
                 "resets_at": (now + week_left).isoformat(), "scope": {"model": {"display_name": "Fable"}}})
    return {"rate_limits": {"limits": rows, "extra_usage": None}}


def reading(week: float = 52.0, week_left: timedelta = timedelta(hours=30), session: Optional[float] = None,
            session_left: Optional[timedelta] = None, at: datetime = NOW) -> Reading:
    windows = []
    if session is not None:
        windows.append(Window("session", "Session", session, resets_at=at + session_left if session_left else None))
    windows.append(Window("weekly_all", "Week (all models)", week, resets_at=at + week_left))
    return Reading(taken_at=at, source="usage_report", windows=tuple(windows))


class QuestionCommandTests(unittest.TestCase):
    """`alfred usage check|running_low|forecast` from inside a bot's turn, with a stand-in `claude`."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        home.prepare(self.sandbox.config)
        self.registry = BotRegistry(self.sandbox.config)
        self.registry.save(BotRecord(bot_id=BOT, username="research_bot", name="Research", owner_id=99, chat_ids=[99],
                                     permissions={"usage": "on"}))
        self.serve(session=40, session_left=timedelta(hours=2), week=90, week_left=timedelta(days=3, minutes=5))

    def serve(self, session: Optional[float] = 40, session_left: Optional[timedelta] = timedelta(hours=2),
              week: float = 39, week_left: timedelta = timedelta(days=2, hours=19), fail: bool = False) -> None:
        # 30 seconds ahead, so "resets in 2h 00m" does not slip to 1h 59m while the test runs
        now = self.served = datetime.now(timezone.utc) + timedelta(seconds=30)
        if fail:
            lines = [{"type": "result", "is_error": True, "result": "rate limited until 3:14am"}]
        else:
            lines = [{"type": "system", "subtype": "init", "session_id": "s"},
                     {"type": "assistant", "message": {"content": []},
                      "usage_report": limits(session, session_left, week, week_left, now)},
                     {"type": "result", "subtype": "success", "is_error": False, "result": "", "total_cost_usd": 0}]
        self.sandbox.fake_cli("claude", {"fresh": lines})

    def forget(self) -> None:
        """Drop the cached readings, so the next question reads /usage again."""
        for path in self.sandbox.config.state_dir.glob("usage.json*"):
            path.unlink()

    def ask(self, *argv: str, bot: Optional[int] = BOT) -> Tuple[int, str, str]:
        environ = dict(self.sandbox.environ, ALFRED_BOT_ID=str(bot) if bot else "")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["usage"] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_verdict(self, output: str, expected: bool) -> str:
        lines = output.splitlines()
        self.assertEqual(len(lines), 1, output)  # one line: the verdict and its reason, nothing else
        verdict, _, reason = lines[0].partition(": ")
        self.assertEqual(verdict, "true" if expected else "false", output)
        return reason

    def test_the_users_example_week_at_90_with_three_days_left(self) -> None:
        code, out, err = self.ask("running_low")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.assert_verdict(out, True), "week 90% used (resets in 3d 0h)")

    def test_the_5_hour_window_and_any_against_one_limit(self) -> None:
        self.serve(session=95, session_left=timedelta(hours=1, minutes=30), week=39)
        self.assertIn("5-hour window 95% used", self.assert_verdict(self.ask("running_low")[1], True))
        self.assertTrue(self.ask("running_low", "--limit", "session")[1].startswith("true: "))
        reason = self.assert_verdict(self.ask("running_low", "--limit", "week")[1], False)
        self.assertEqual(reason, "week 39% used, needs >= 90 (resets in 2d 19h)")

    def test_the_threshold(self) -> None:
        self.assert_verdict(self.ask("running_low", "--threshold", "95")[1], False)
        self.assert_verdict(self.ask("running_low", "--threshold", "90")[1], True)
        self.sandbox.environ["ALFRED_USAGE_LOW_THRESHOLD"] = "95"
        self.assert_verdict(self.ask("running_low")[1], False)
        self.assertEqual(self.ask("running_low", "--threshold", "0")[0], cli.EXIT_BAD_QUESTION)

    def test_json_has_the_breakdown(self) -> None:
        code, out, _ = self.ask("running_low", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["result"], data["low"], data["threshold"]), (True, ["week"], 90.0))
        self.assertEqual(data["reason"], "week 90% used (resets in 3d 0h)")
        week, session = data["limits"]["week"], data["limits"]["session"]
        self.assertEqual((week["used"], week["left"], week["resets_in"]), (90.0, 10.0, "3d 0h"))
        self.assertEqual((session["used"], session["left"], session["resets_in"]), (40.0, 60.0, "2h 00m"))
        self.assertEqual(data["limits"]["week_fable"]["used"], 59.0)
        self.assertIn("resets_at", week)
        self.assertEqual(data["condition"], "session.used >= 90 or week.used >= 90")

    def test_check_with_several_conditions(self) -> None:
        for condition, expected, reason in (
            ("week.used >= 90", True, "week 90% used (resets in 3d 0h)"),
            ("week.used >= 95", False, "week 90% used, needs >= 95 (resets in 3d 0h)"),
            ("session.left < 1h and week.used > 50", False, "5-hour window resets in 2h 00m, needs < 1h"),
            ("session.used < 50 and week.left > 2d", True, "5-hour window 40% used (resets in 2h 00m); week resets in 3d 0h"),
            ("not week.used < 80", True, "week 90% used, needs < 80 (resets in 3d 0h)"),
            ("week.surplus > 0", False, None),
            ("week.windows_left >= 14", True, None),
        ):
            code, out, err = self.ask("check", condition)
            self.assertEqual((code, err), (0, ""), condition)
            got = self.assert_verdict(out, expected)
            if reason is not None:
                self.assertEqual(got, reason, condition)

    def test_a_bad_condition_is_an_error_not_a_false(self) -> None:
        for condition in ("week.used >= lots", "fable.used > 3", "session.surplus > 0", "week.used >"):
            code, out, err = self.ask("check", condition)
            self.assertEqual((code, out), (cli.EXIT_BAD_QUESTION, ""), condition)
            self.assertIn("bad condition", err)
        code, out, _ = self.ask("check", "week.used >= lots", "--json")
        self.assertEqual(code, cli.EXIT_BAD_QUESTION)
        self.assertEqual(json.loads(out)["ok"], False)
        self.assertEqual(self.sandbox.calls("claude"), [])  # refused before anything was read

    def test_a_failed_reading_is_its_own_exit_code(self) -> None:
        self.serve(fail=True)
        code, out, err = self.ask("running_low")
        self.assertEqual((code, out), (cli.EXIT_UNREAD, ""))
        self.assertIn("could not read the plan usage", err)

    def test_readings_are_cached_and_capped(self) -> None:
        for _ in range(4):
            self.assertEqual(self.ask("running_low")[0], 0)
        self.assertEqual(self.ask("check", "week.used > 3")[0], 0)
        self.assertEqual(len(self.sandbox.calls("claude")), 1)  # one /usage for five questions
        self.assertTrue(json.loads(self.ask("running_low", "--json")[1])["reading"]["cached"])
        # older than ALFRED_USAGE_MAX_AGE: read again
        history = UsageHistory(self.sandbox.config.state_dir / "usage.json")
        old = history.readings()[-1]
        self.forget()
        history.append(Reading(old.taken_at - timedelta(minutes=10), old.source, old.windows), now=old.taken_at)
        self.ask("running_low")
        self.assertEqual(len(self.sandbox.calls("claude")), 2)
        # after a failed reading, nobody asks again for ALFRED_USAGE_RETRY
        self.forget()
        self.serve(fail=True)
        self.assertEqual(self.ask("running_low")[0], cli.EXIT_UNREAD)
        self.assertEqual(self.ask("running_low")[0], cli.EXIT_UNREAD)
        self.assertEqual(len(self.sandbox.calls("claude")), 3)

    def test_works_with_the_monitor_off_but_needs_the_permission(self) -> None:
        self.assertNotEqual(self.sandbox.environ.get("ALFRED_USAGE_MONITOR"), "1")
        self.assertEqual(self.ask("running_low")[0], 0)
        record = self.registry.get(BOT)
        record.permissions["usage"] = "off"
        self.registry.save(record)
        for argv in (("running_low",), ("check", "week.used > 3"), ("forecast",), ("running_low", "--json")):
            code, out, err = self.ask(*argv)
            self.assertEqual(code, cli.EXIT_REFUSED, argv)
            self.assertIn("usage permission", err + out)
        self.assertEqual(len(self.sandbox.calls("claude")), 1)  # only the question asked with the permission

    def test_claude_code_rules_cover_the_new_commands(self) -> None:
        record = BotRecord(bot_id=BOT, username="research_bot", name="Research", owner_id=99, chat_ids=[99])
        deny = bots.claude_settings(self.sandbox.config, record)["permissions"]["deny"]
        self.assertIn("Bash(alfred usage *)", deny)  # running_low, check, forecast
        self.assertIn("Bash(alfred usage-alert *)", deny)  # waste
        record.permissions.update({"usage": "on", "shell": "full"})
        excluded = bots.claude_settings(self.sandbox.config, record)["sandbox"]["excludedCommands"]
        self.assertIn("alfred usage *", excluded)

    def test_no_model_call(self) -> None:
        self.ask("running_low")
        self.forget()
        self.ask("forecast", "--json")
        for call in self.sandbox.calls("claude"):
            self.assertEqual(call["stdin"], "/usage")
            self.assertIn("stream-json", call["argv"])

    def test_forecast_says_whether_quota_will_be_lost(self) -> None:
        self.serve(session=None, session_left=None, week=52, week_left=timedelta(hours=30, seconds=30))
        reason = self.assert_verdict(self.ask("forecast")[1], True)  # the exact limit counts by default
        self.assertIn("48% of the week is left and the 6 useful 5-hour windows", reason)
        self.assertIn("at most 48%: 0% will be lost", reason)
        self.assert_verdict(self.ask("forecast", "--margin", "1")[1], False)
        data = json.loads(self.ask("forecast", "--json")[1])
        forecast = data["forecast"]
        self.assertEqual((forecast["windows_left"], forecast["max_spend"], forecast["surplus"]), (6.0, 48.0, 0.0))
        self.assertEqual(len(forecast["windows"]), 7)  # six whole, and a sliver of 30 seconds
        self.assertTrue(any("first message" in item for item in forecast["assumptions"]))
        self.assertEqual(data["window_share"]["value"], 8.0)
        self.assertIn("default", data["window_share"]["source"])
        self.assertEqual(self.ask("forecast", "--hours", "9-75")[0], cli.EXIT_BAD_QUESTION)

    def test_projected_pace(self) -> None:
        self.serve(session=50, session_left=timedelta(hours=2, minutes=30), week=60)
        before = self.served - timedelta(minutes=30)  # the same window, 30 minutes earlier, at 20%
        history = UsageHistory(self.sandbox.config.state_dir / "usage.json")
        history.append(reading(week=59, week_left=timedelta(days=2, hours=19, minutes=30), session=20,
                               session_left=timedelta(hours=3), at=before), now=before)
        self.assert_verdict(self.ask("running_low", "--limit", "session")[1], False)
        reason = self.assert_verdict(self.ask("running_low", "--limit", "session", "--projected")[1], True)
        self.assertIn("at the recent pace the 5-hour window reaches", reason)


class ForecastConditionTests(unittest.TestCase):
    def evaluate(self, condition: str, current: Reading, hours: Optional[str] = None,
                 settings: Optional[MonitorSettings] = None) -> Tuple[bool, str]:
        evaluation = Evaluation(settings or MonitorSettings(), current, NOW)
        node = parse(condition)
        values, _ = evaluation.values(node, hours)
        return usage_alerts.verdict(node, values)

    def test_the_new_values(self) -> None:
        current = reading(week=52, week_left=timedelta(hours=30))
        for condition, expected in (
            ("week.windows_left = 6", True), ("week.windows < 6", False), ("week.max_spend >= 48%", True),
            ("week.surplus >= 0", True), ("week.surplus > 0", False), ("week.surplus > -1", True),
            ("week.surplus < -0.5", False),
        ):
            self.assertEqual(self.evaluate(condition, current)[0], expected, condition)

    def test_active_hours_change_the_answer(self) -> None:
        current = reading(week=52, week_left=timedelta(hours=30))
        self.assertFalse(self.evaluate("week.surplus > 0", current)[0])
        result, reason = self.evaluate("week.surplus > 0", current, "9-20 UTC")
        self.assertTrue(result)
        self.assertEqual(reason, "20.8% of the week will go unused even using every remaining window fully "
                                 "(resets in 1d 6h)")
        defaults = MonitorSettings(active_hours=usage_forecast_hours("9-20 UTC"))
        self.assertTrue(self.evaluate("week.surplus > 0", current, settings=defaults)[0])

    def test_refused_forms(self) -> None:
        for condition, reason in (("session.windows_left > 1", "about the week"), ("week.surplus > lots", "number"),
                                  ("week.windows_left > 3%", "plain number")):
            with self.assertRaises(ConditionError) as caught:
                parse(condition)
            self.assertIn(reason, str(caught.exception))

    def test_configured_share_is_never_replaced_by_a_measure(self) -> None:
        history = []
        for window in range(2):
            start = NOW - timedelta(hours=10) + timedelta(hours=5 * window)
            for step in range(4):
                at = start + timedelta(hours=step)
                history.append(reading(week=30 + 5 * window + 5 * step / 3, week_left=NOW + timedelta(days=2) - at,
                                       session=20.0 * step, session_left=timedelta(hours=5 - step), at=at))
        measured = Evaluation(MonitorSettings(), reading(), NOW, history).share
        self.assertEqual((measured.value, measured.measured.share), (8.3, 8.3))
        self.assertIn("measured", measured.source)
        configured = Evaluation(MonitorSettings(window_share_percent=10, window_share_configured=True), reading(), NOW,
                                history).share
        self.assertEqual((configured.value, configured.measured.share), (10, 8.3))
        self.assertIn("ALFRED_USAGE_WINDOW_SHARE", configured.source)
        off = Evaluation(MonitorSettings(calibrate=False), reading(), NOW, history).share
        self.assertEqual((off.value, off.measured), (8.0, None))


def usage_forecast_hours(text: str) -> Any:
    from alfred.usage_forecast import parse_hours

    return parse_hours(text)


class ForecastAlertTests(unittest.TestCase):
    """Forecast alerts in the monitor: once per quota week, again only when the loss grows."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        self.sandbox.environ["ALFRED_USAGE_MONITOR"] = "1"
        home.prepare(self.sandbox.config)
        self.config = self.sandbox.config
        self.registry = BotRegistry(self.config)
        for bot_id, username in ((BOT, "research_bot"), (OTHER, "chef_bot")):
            self.registry.save(BotRecord(bot_id=bot_id, username=username, name=username, owner_id=99, chat_ids=[99],
                                         permissions={"usage": "on"}))
        self.scheduler = SchedulerStore(self.config.db_path)
        self.source = FixedSource(reading())
        self.monitor = UsageMonitor(MonitorSettings.from_config(self.config), self.source,
                                    UsageHistory(self.config.state_dir / "usage.json"), config=self.config,
                                    scheduler=self.scheduler)
        self.monitor.settings.wake_interval_seconds = 5 * 60

    def run_json(self, *argv: str, bot: int = BOT) -> Dict[str, Any]:
        environ = dict(self.sandbox.environ, ALFRED_BOT_ID=str(bot), ALFRED_CHAT_ID="99", ALFRED_USER_ID="99",
                       ALFRED_USERNAME="pablo", ALFRED_THREAD_ID="")
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, environ), redirect_stdout(buffer):
            cli.main(list(argv))
        return json.loads(buffer.getvalue())

    def cycle(self, now: datetime, current: Reading) -> None:
        self.source.current = current
        asyncio.run(self.monitor.cycle(now))

    def events(self, bot_id: int = BOT) -> List[Dict[str, Any]]:
        return [item for item in self.scheduler.list(99, bot_id=bot_id, include_done=True) if item["kind"] == KIND_EVENT]

    def test_the_users_command_and_the_waste_shortcut(self) -> None:
        added = self.run_json("usage-alert", "add", "week.surplus > 0", "--hours", "9-20")
        self.assertTrue(added["ok"], added)
        self.assertEqual((added["alert"]["when"], added["alert"]["hours"]), ("week.surplus > 0", "09:00-20:00"))
        self.assertIn("once per quota week", added["fires"])
        waste = self.run_json("usage-alert", "waste", "--hours", "9-20", "--days", "mon-fri", "--margin", "5",
                              "--repeat", "run", "the", "backlog")
        self.assertTrue(waste["ok"], waste)
        self.assertEqual(waste["alert"]["when"], "week.surplus >= 5 and week.used < 100")
        self.assertEqual((waste["alert"]["hours"], waste["alert"]["note"], waste["alert"]["repeat"]),
                         ("09:00-20:00 mon-fri", "run the backlog", True))
        self.assertIn("active hours only change", self.run_json("usage-alert", "add", "week.used > 3", "--hours", "9-20")["error"])
        self.assertFalse(self.run_json("usage-alert", "add", "week.surplus > 0", "--hours", "25-3")["ok"])

    def test_waste_needs_the_permission_and_the_monitor(self) -> None:
        record = self.registry.get(BOT)
        record.permissions["usage"] = "off"
        self.registry.save(record)
        self.assertIn("usage permission", self.run_json("usage-alert", "waste")["error"])
        self.sandbox.environ["ALFRED_USAGE_MONITOR"] = "0"
        self.assertIn("usage monitor is off", self.run_json("usage-alert", "waste", bot=OTHER)["error"])

    def test_the_users_example_fires_at_the_exact_limit_with_a_clear_event(self) -> None:
        self.run_json("usage-alert", "waste", "use", "the", "spare", "quota")
        self.run_json("usage-alert", "waste", "--margin", "1", bot=OTHER)  # one point of slack: not yet
        self.cycle(NOW, reading(week=52, week_left=timedelta(hours=30)))
        (event,) = self.events()
        self.assertEqual(self.events(OTHER), [])
        text = event["message"]
        for expected in ("Usage alert met", "week.surplus >= 0 and week.used < 100", "use the spare quota",
                         "Verdict: Nothing to spare", "Left this week: 48%, resets", "(in 1d 6h)",
                         "Useful 5-hour windows left: 6 (active hours: all day, every day)",
                         "Most that can still be spent: 48% (6 windows x 8% of the week each",
                         "Will be lost: 0%", "once per quota week", "spent when this week resets",
                         "act on it; there is no need to read or recompute"):
            self.assertIn(expected, text)

    def test_once_per_week_again_when_the_loss_grows_and_spent_after_the_week(self) -> None:
        self.run_json("usage-alert", "add", "week.surplus > 0", "--hours", "9-20 UTC")
        self.run_json("usage-alert", "add", "week.surplus > 0", "--hours", "9-20 UTC", "--repeat", bot=OTHER)
        self.cycle(NOW, reading(week=52, week_left=timedelta(hours=30)))  # 20.8% lost
        self.assertEqual((len(self.events()), len(self.events(OTHER))), (1, 1))
        later = NOW + timedelta(minutes=15)  # the same week, the same loss: no new wake-up
        self.cycle(later, reading(week=52, week_left=timedelta(hours=29, minutes=45), at=later))
        self.assertEqual(len(self.events()), 1)
        evening = NOW + timedelta(hours=8)  # 18:00, still the same week: the loss grew by more than one window
        self.cycle(evening, reading(week=52, week_left=timedelta(hours=22), at=evening))
        self.assertEqual(len(self.events()), 2)
        self.assertIn("Will be lost: 33.6%", self.events()[-1]["message"])
        next_week = NOW + timedelta(days=7)
        self.cycle(next_week, reading(week=52, week_left=timedelta(hours=30), at=next_week))
        self.assertEqual(len(self.events()), 2)  # one-shot: spent when its week reset
        self.assertTrue(AlertStore.for_bot(self.config, BOT).alerts()[0].is_spent(next_week))
        self.assertFalse(AlertStore.for_bot(self.config, OTHER).alerts()[0].is_spent(next_week))
        self.assertEqual(len(self.events(OTHER)), 3)  # --repeat: armed again every week

    def test_held_by_the_wake_interval_without_being_spent(self) -> None:
        self.monitor.settings.wake_interval_seconds = 6 * 3600
        self.run_json("usage-alert", "add", "week.used > 10")
        self.run_json("usage-alert", "waste")
        self.cycle(NOW, reading(week=30, week_left=timedelta(days=3)))  # only the first one holds
        self.cycle(NOW + timedelta(hours=1), reading(week=52, week_left=timedelta(hours=29), at=NOW + timedelta(hours=1)))
        self.assertEqual(len(self.events()), 1)  # waste holds, but the bot was woken an hour ago
        self.cycle(NOW + timedelta(hours=7), reading(week=55, week_left=timedelta(hours=23), at=NOW + timedelta(hours=7)))
        self.assertEqual(len(self.events()), 2)
        self.assertIn("Will be lost", self.events()[-1]["message"])


class SkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (extensions.BASE_PLUGIN / "skills" / "usage" / "SKILL.md").read_text(encoding="utf-8")

    def test_the_skill_teaches_the_verdict_pattern(self) -> None:
        for expected in ("alfred usage running_low", "alfred usage check", "alfred usage forecast",
                         "alfred usage-alert waste", "verdict", "do not recompute", "NO_REPLY", "urgent",
                         "exit code 3", "week.used >= 90", "--json"):
            self.assertIn(expected, self.text)

    def test_the_skill_translates_the_users_rules(self) -> None:
        self.assertIn("SOUL", self.text)
        self.assertIn("memory", self.text)
        self.assertIn("can't be expressed", self.text)


if __name__ == "__main__":
    unittest.main()
