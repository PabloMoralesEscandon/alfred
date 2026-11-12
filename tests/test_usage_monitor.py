"""The usage monitor skeleton: off by default and inert, and every piece can be built."""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List
from unittest import mock

from alfred import usage_monitor
from alfred.usage_monitor import (
    Alert,
    ClaudeUsageSource,
    MonitorSettings,
    Reading,
    Rule,
    RuleContext,
    UsageHistory,
    UsageMonitor,
    UsageSource,
    Window,
)
from fakes import Sandbox

try:
    from alfred.telegram_bot import TelegramBot
except ImportError:  # pragma: no cover
    TelegramBot = None  # type: ignore

REPORT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 33% used · resets Nov 12 at 12:40pm (Europe/Berlin)\n"
    "Current week (all models): 39% used · resets Nov 15 at 6am (Europe/Berlin)\n"
    "Current week (Fable): 59% used · resets Nov 15 at 6am (Europe/Berlin)\n\n"
    "What's contributing to your usage?\n  92%  Stop sessions you no longer need\n"
)
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


class Captured(logging.Handler):
    """Collects the monitor's log records (assertNoLogs needs Python 3.10)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class SkeletonTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.home.mkdir()
        # A stand-in claude that would answer /usage, so any call to it shows up in calls().
        self.sandbox.fake_cli("claude", {"fresh": [{"type": "result", "is_error": False, "result": REPORT}]})
        self.logs = Captured()
        logger = logging.getLogger("alfred.usage_monitor")
        logger.addHandler(self.logs)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, previous)
        self.addCleanup(logger.removeHandler, self.logs)

    def files_in_home(self) -> List[str]:
        return sorted(str(path.relative_to(self.sandbox.home)) for path in self.sandbox.home.rglob("*"))


class DisabledTests(SkeletonTestCase):
    def test_off_by_default_and_does_nothing(self) -> None:
        before = self.files_in_home()
        with mock.patch.object(MonitorSettings, "from_config", side_effect=AssertionError("read settings")), \
                mock.patch.object(usage_monitor.usage, "fetch_report", side_effect=AssertionError("read usage")):
            self.assertIsNone(usage_monitor.start(self.sandbox.config))
        self.assertFalse(usage_monitor.is_enabled(self.sandbox.config))
        self.assertEqual(self.sandbox.calls("claude"), [])
        self.assertEqual(self.files_in_home(), before)
        self.assertEqual(self.logs.records, [])

    def test_off_values(self) -> None:
        for value in ("0", "false", "no", "off"):
            self.sandbox.environ["ALFRED_USAGE_MONITOR"] = value
            self.assertIsNone(usage_monitor.start(self.sandbox.config), value)
        self.assertEqual(self.sandbox.calls("claude"), [])

    def test_switch_is_read_from_the_env_file_too(self) -> None:
        (self.sandbox.home / ".env").write_text("ALFRED_USAGE_MONITOR=1\n", encoding="utf-8")
        self.assertTrue(usage_monitor.is_enabled(self.sandbox.config))


class EnabledTests(SkeletonTestCase):
    def test_on_only_logs_one_line(self) -> None:
        self.sandbox.environ["ALFRED_USAGE_MONITOR"] = "1"
        before = self.files_in_home()
        with mock.patch.object(usage_monitor.usage, "fetch_report", side_effect=AssertionError("read usage")):
            monitor = usage_monitor.start(self.sandbox.config)
        self.assertIsInstance(monitor, UsageMonitor)
        assert monitor is not None
        self.assertTrue(monitor.settings.enabled)
        self.assertEqual(len(self.logs.records), 1)
        self.assertIn("not implemented yet", self.logs.records[0].getMessage())
        self.assertEqual(self.sandbox.calls("claude"), [])
        self.assertEqual(self.files_in_home(), before)  # no history file written

    def test_the_six_rules_are_declared_and_never_alert(self) -> None:
        rules = usage_monitor.default_rules()
        self.assertEqual(
            [rule.name for rule in rules],
            ["threshold", "projection", "limit_reached", "reset", "spare_session", "spare_weekly"],
        )
        context = RuleContext(now=NOW, reading=Reading.from_report(REPORT, NOW), history=[], settings=MonitorSettings())
        for rule in rules:
            self.assertIsInstance(rule, Rule)
            self.assertFalse(rule.implemented, rule.name)
            self.assertTrue(rule.summary, rule.name)
            self.assertEqual(rule.evaluate(context), [], rule.name)


class ReadingTests(SkeletonTestCase):
    def test_reading_from_the_usage_text(self) -> None:
        reading = Reading.from_report(REPORT, NOW)
        self.assertTrue(reading.ok)
        self.assertEqual(reading.source, "text")
        self.assertEqual(
            [(window.kind, window.label, window.percent, window.scope) for window in reading.windows],
            [
                ("session", "Session", 33.0, None),
                ("weekly_all", "Week (all models)", 39.0, None),
                ("weekly_scoped", "Week (Fable)", 59.0, "Fable"),
            ],
        )
        self.assertEqual(reading.windows[0].reset_text, "Nov 12 at 12:40pm")
        self.assertIsNone(reading.windows[0].resets_at)  # the text has no year or offset

    def test_unknown_meters_are_kept_and_other_formats_give_no_windows(self) -> None:
        reading = Reading.from_report("Current day (beta): 5% used\n", NOW)
        self.assertEqual([(window.kind, window.label) for window in reading.windows], [("day (beta)", "Day (beta)")])
        self.assertEqual(Reading.from_report("Total cost: $1.23", NOW).windows, ())

    def test_round_trip(self) -> None:
        window = Window("session", "Session", 33.0, resets_at=NOW + timedelta(hours=1), severity="normal")
        reading = Reading(taken_at=NOW, source="usage_report", windows=(window,))
        self.assertEqual(Reading.from_dict(reading.to_dict()), reading)
        failed = Reading.failed("Not logged in", "text", NOW)
        self.assertFalse(failed.ok)
        self.assertEqual(Reading.from_dict(failed.to_dict()), failed)

    def test_alert_can_be_built(self) -> None:
        alert = Alert(rule="threshold", key="threshold:session:80", text="Session at 82%")
        self.assertFalse(alert.urgent)


class SourceTests(SkeletonTestCase):
    def test_claude_source_reuses_usage_and_reads_the_report(self) -> None:
        source = ClaudeUsageSource(self.sandbox.config, dict(self.sandbox.environ))
        self.assertIsInstance(source, UsageSource)
        reading = asyncio.run(source.read())
        self.assertTrue(reading.ok)
        self.assertEqual(len(reading.windows), 3)
        (call,) = self.sandbox.calls("claude")
        self.assertEqual(call["stdin"], "/usage")

    def test_claude_source_reports_failures(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": [{"type": "result", "is_error": True, "result": "Not logged in\nRun /login"}]})
        reading = asyncio.run(ClaudeUsageSource(self.sandbox.config, dict(self.sandbox.environ)).read())
        self.assertEqual(reading.error, "Not logged in")
        self.assertEqual(reading.windows, ())

    def test_base_source_is_an_interface(self) -> None:
        with self.assertRaises(NotImplementedError):
            asyncio.run(UsageSource().read())


class HistoryTests(SkeletonTestCase):
    def test_nothing_on_disk_until_used(self) -> None:
        history = UsageHistory(self.sandbox.root / "state" / "usage.json")
        self.assertFalse(history.path.exists())
        self.assertEqual(history.readings(), [])
        self.assertIsNone(history.latest())

    def test_append_keeps_recent_readings_in_order(self) -> None:
        history = UsageHistory(self.sandbox.root / "state" / "usage.json", keep=timedelta(hours=24))
        old = Reading.from_report(REPORT, NOW - timedelta(hours=30))
        first = Reading.from_report(REPORT, NOW - timedelta(hours=1))
        second = Reading.failed("rate limited", "text", NOW)
        history.append(old, now=old.taken_at)
        history.append(first, now=first.taken_at)
        history.append(second, now=NOW)
        self.assertEqual(history.readings(), [first, second])  # the 30-hour-old one was dropped
        self.assertEqual(history.latest(), second)
        self.assertEqual(UsageHistory(history.path).readings(), [first, second])

    def test_damaged_file_or_entry_is_survivable(self) -> None:
        path = self.sandbox.root / "usage.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(UsageHistory(path).readings(), [])
        good = Reading.from_report(REPORT, NOW).to_dict()
        path.write_text('{"readings": [{"source": "text"}, %s]}' % json.dumps(good), encoding="utf-8")
        self.assertEqual(len(UsageHistory(path).readings()), 1)


class SettingsTests(SkeletonTestCase):
    def test_defaults(self) -> None:
        settings = MonitorSettings.from_config(self.sandbox.config)
        self.assertFalse(settings.enabled)
        self.assertIsNone(settings.chat_id)
        self.assertEqual(settings.poll_seconds, 900)
        self.assertEqual(settings.warn_percents, (80, 95))
        self.assertEqual(settings.cap_percent, 95)
        self.assertEqual(settings.quiet_hours, (23, 8))
        self.assertTrue(settings.spare)
        self.assertFalse(settings.autorun)
        self.assertEqual(settings.history_path, self.sandbox.config.state_dir / "usage.json")

    def test_values_are_parsed(self) -> None:
        self.sandbox.environ.update({
            "ALFRED_USAGE_MONITOR": "1",
            "ALFRED_USAGE_CHAT_ID": "12345",
            "ALFRED_USAGE_POLL": "30m",
            "ALFRED_USAGE_WARN": "95, 70%",
            "ALFRED_USAGE_CAP": "off",
            "ALFRED_QUIET_HOURS": "22-7",
            "ALFRED_USAGE_SPARE": "0",
            "ALFRED_USAGE_AUTORUN": "1",
        })
        settings = MonitorSettings.from_config(self.sandbox.config)
        self.assertEqual(
            (settings.enabled, settings.chat_id, settings.poll_seconds, settings.warn_percents, settings.cap_percent,
             settings.quiet_hours, settings.spare, settings.autorun),
            (True, 12345, 1800, (70, 95), None, (22, 7), False, True),
        )

    def test_bad_values_keep_their_defaults(self) -> None:
        self.sandbox.environ.update({
            "ALFRED_USAGE_POLL": "1m",
            "ALFRED_USAGE_WARN": "lots",
            "ALFRED_USAGE_CAP": "150",
            "ALFRED_QUIET_HOURS": "late",
        })
        settings = MonitorSettings.from_config(self.sandbox.config)
        self.assertEqual(
            (settings.poll_seconds, settings.warn_percents, settings.cap_percent, settings.quiet_hours),
            (900, (80, 95), 95, (23, 8)),
        )
        self.assertEqual(len(self.logs.records), 4)

    def test_chat_id_falls_back_to_the_only_allowed_id(self) -> None:
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "123456789, somebody"
        self.assertEqual(MonitorSettings.from_config(self.sandbox.config).chat_id, 123456789)
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "123456789,987654321"
        self.assertIsNone(MonitorSettings.from_config(self.sandbox.config).chat_id)


@unittest.skipIf(TelegramBot is None, "python-telegram-bot is not installed")
class BotWiringTests(SkeletonTestCase):
    def post_init(self) -> Any:
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
        app = TelegramBot(self.sandbox.config)

        async def set_my_commands(commands: Any) -> None:
            pass

        async def run() -> None:
            await app._post_init(SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_my_commands)))
            app._scheduler.cancel()
            await app.agent.shutdown()

        asyncio.run(run())
        return app

    def test_bot_start_leaves_the_monitor_off(self) -> None:
        app = self.post_init()
        self.assertIsNone(app.usage_monitor)
        self.assertEqual(self.sandbox.calls("claude"), [])
        self.assertEqual(self.logs.records, [])

    def test_bot_start_with_the_monitor_on_only_logs(self) -> None:
        self.sandbox.environ["ALFRED_USAGE_MONITOR"] = "1"
        app = self.post_init()
        self.assertIsInstance(app.usage_monitor, UsageMonitor)
        self.assertEqual(len(self.logs.records), 1)
        self.assertEqual(self.sandbox.calls("claude"), [])


if __name__ == "__main__":
    unittest.main()
