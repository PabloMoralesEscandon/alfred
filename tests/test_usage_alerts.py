"""Usage alerts for agents: conditions, one firing per window, wake limits, isolation between bots,
the `alfred usage-alert` command, and the whole path through the pool with stand-in Telegram and Claude."""

from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest import mock

from consiglieri import cli, home, usage_alerts, usage_monitor
from consiglieri.bots import BotRecord, BotRegistry
from consiglieri.scheduler import KIND_EVENT, SchedulerStore
from consiglieri.usage_alerts import AlertStore, ConditionError, parse
from consiglieri.usage_monitor import (ClaudeUsageSource, MonitorSettings, Reading, SpareWeeklyRule, UsageHistory,
                                  UsageMonitor, UsageSource, Window)
from fakes import Sandbox

try:
    from consiglieri.telegram_bot import BotPool
    from test_panel import OWNER, TestPool, token_for
except ImportError:  # pragma: no cover
    BotPool = None  # type: ignore

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
RESEARCH = 5000000001
CHEF = 5000000002


def reading(session: Optional[float] = 0.0, session_left: Optional[timedelta] = timedelta(minutes=50),
            week: float = 39.0, week_left: timedelta = timedelta(days=2, hours=19), at: datetime = NOW) -> Reading:
    windows = []
    if session is not None:
        windows.append(Window("session", "Session", session, resets_at=at + session_left if session_left else None))
    windows.append(Window("weekly_all", "Week (all models)", week, resets_at=at + week_left))
    windows.append(Window("weekly_scoped", "Week (Fable)", 59.0, resets_at=at + week_left, scope="Fable"))
    return Reading(taken_at=at, source="usage_report", windows=tuple(windows))


def check(condition: str, current: Reading, now: datetime = NOW) -> bool:
    return parse(condition).check(usage_alerts.values_of(current, now))


class ConditionTests(unittest.TestCase):
    def test_the_users_example(self) -> None:
        condition = "session.left < 1h and session.used = 0"
        self.assertTrue(check(condition, reading(session=0, session_left=timedelta(minutes=50))))
        self.assertFalse(check(condition, reading(session=0, session_left=timedelta(minutes=70))))
        self.assertFalse(check(condition, reading(session=3, session_left=timedelta(minutes=50))))
        self.assertFalse(check(condition, reading(session=None)))  # no 5-hour window running: nothing to compare

    def test_each_value_and_operator(self) -> None:
        current = reading(session=40, session_left=timedelta(hours=2), week=75, week_left=timedelta(hours=20))
        for condition, expected in (
            ("session.used > 30", True), ("session.used >= 40", True), ("session.used < 40", False),
            ("session.used <= 40%", True), ("session.used != 40", False), ("session.used == 40", True),
            ("week.used > 80", False), ("week.used < 80", True),
            ("session.left < 3h", True), ("session.left > 90m", True), ("session.left <= 1h", False),
            ("week.left < 1d", True), ("week.reset < 1d", True), ("5h.used > 30", True), ("weekly.used < 80", True),
        ):
            self.assertEqual(check(condition, current), expected, condition)

    def test_combinations_with_and_or_not_and_parentheses(self) -> None:
        current = reading(session=40, session_left=timedelta(hours=2), week=75, week_left=timedelta(hours=20))
        for condition, expected in (
            ("session.used > 30 and week.used > 70", True),
            ("session.used > 50 and week.used > 70", False),
            ("session.used > 50 or week.used > 70", True),
            ("session.used > 50 or week.used > 90", False),
            ("session.used > 50 or week.used > 70 and week.left < 1d", True),  # and binds tighter
            ("(session.used > 50 or week.used > 70) and week.left > 1d", False),
            ("not session.used > 50", True),
            ("session.used > 30 && (week.left < 1d || week.used > 99)", True),
            ("week.left < 1d and session.left < 3h and session.used < 50 and week.used < 80", True),
        ):
            self.assertEqual(check(condition, current), expected, condition)

    def test_a_failed_reading_matches_nothing(self) -> None:
        failed = Reading.failed("rate limited", "usage_report", NOW)
        self.assertFalse(check("session.used < 100 or week.used < 100", failed))
        self.assertEqual(usage_alerts.due([usage_alerts.Alert(id="a", when="week.used < 100")], failed, NOW), [])

    def test_bad_conditions_are_refused_with_a_reason(self) -> None:
        for condition, reason in (
            ("", "empty"), ("session.used", "ends too early"), ("session.cost > 3", "unknown value"),
            ("month.used > 3", "unknown value"), ("session.used ~ 3", "can't read"),
            ("session.used > lots", "percentage"), ("session.used > 120", "0 to 100"),
            ("session.left < 60", "time such as"), ("(session.used > 3", "ends too early"),
            ("session.used > 3 week.used > 3", "unexpected"), ("x" * 400, "longer than"),
        ):
            with self.assertRaises(ConditionError, msg=condition) as caught:
                parse(condition)
            self.assertIn(reason, str(caught.exception), condition)

    def test_window_key_follows_the_windows_the_condition_names(self) -> None:
        first = reading(session_left=timedelta(minutes=50))
        wobble = Reading(taken_at=NOW, source="usage_report", windows=tuple(
            Window(window.kind, window.label, window.percent, resets_at=window.resets_at + timedelta(seconds=0.3)
                   if window.resets_at else None) for window in first.windows))
        node = parse("session.used = 0")
        self.assertEqual(usage_alerts.window_key(node, first), usage_alerts.window_key(node, wobble))
        later = reading(session_left=timedelta(minutes=50), at=NOW + timedelta(hours=5))
        self.assertNotEqual(usage_alerts.window_key(node, first), usage_alerts.window_key(node, later))
        week_node = parse("week.used < 50")
        self.assertEqual(usage_alerts.window_key(week_node, first),
                         usage_alerts.window_key(week_node, reading(at=NOW, session_left=timedelta(hours=3))))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.store = AlertStore(self.sandbox.root / "bots" / "1" / "usage-alerts.json")

    def test_add_list_remove(self) -> None:
        alert = self.store.add("session.left < 1h and  session.used = 0", note="use the spare window", chat_id=99)
        self.assertEqual(alert.when, "session.left < 1h and session.used = 0")
        self.assertEqual([item.id for item in AlertStore(self.store.path).alerts()], [alert.id])
        self.assertIsNone(self.store.remove("zzz"))
        self.assertEqual(self.store.remove(alert.id[:4]).id, alert.id)
        self.assertEqual(self.store.alerts(), [])

    def test_bad_conditions_are_not_stored(self) -> None:
        with self.assertRaises(ConditionError):
            self.store.add("session.used > lots", chat_id=99)
        self.assertFalse(self.store.path.exists())

    def test_a_bot_has_a_limited_number(self) -> None:
        for _ in range(usage_alerts.MAX_ALERTS_PER_BOT):
            self.store.add("week.used > 90", chat_id=99)
        with self.assertRaises(ConditionError):
            self.store.add("week.used > 90", chat_id=99)

    def test_one_shot_fires_once_even_after_a_restart(self) -> None:
        self.store.add("session.used = 0", chat_id=99)
        firings = usage_alerts.due(self.store.alerts(), reading(), NOW)
        self.assertEqual(len(firings), 1)
        fired = usage_alerts.mark_fired(self.store, firings, NOW)
        self.assertTrue(fired[0].spent)
        restarted = AlertStore(self.store.path)  # a new process reads the same file
        self.assertEqual(usage_alerts.due(restarted.alerts(), reading(at=NOW + timedelta(hours=6)), NOW + timedelta(hours=6)), [])

    def test_repeat_fires_once_per_window(self) -> None:
        self.store.add("session.used = 0", repeat=True, chat_id=99)
        first = reading()
        usage_alerts.mark_fired(self.store, usage_alerts.due(self.store.alerts(), first, NOW), NOW)
        same_window = NOW + timedelta(minutes=20)
        again = reading(session_left=timedelta(minutes=30), at=same_window)
        self.assertEqual(usage_alerts.due(AlertStore(self.store.path).alerts(), again, same_window), [])
        next_window = NOW + timedelta(hours=5)
        self.assertEqual(len(usage_alerts.due(self.store.alerts(), reading(at=next_window), next_window)), 1)


class FixedSource(UsageSource):
    def __init__(self, current: Reading) -> None:
        self.current = current
        self.reads = 0

    async def read(self) -> Reading:
        self.reads += 1
        return self.current


class MonitorCycleTests(unittest.TestCase):
    """One reading per cycle, checked against every bot's alerts; each bot woken only by its own."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["CONSIGLIERI_USAGE_MONITOR"] = "1"
        home.prepare(self.sandbox.config)
        self.config = self.sandbox.config
        self.registry = BotRegistry(self.config)
        for bot_id, username in ((RESEARCH, "research_bot"), (CHEF, "chef_bot")):
            self.registry.save(BotRecord(bot_id=bot_id, username=username, name=username, owner_id=99, chat_ids=[99],
                                         permissions={"usage": "on"}))
        self.scheduler = SchedulerStore(self.config.db_path)
        self.source = FixedSource(reading())
        self.monitor = UsageMonitor(MonitorSettings.from_config(self.config), self.source,
                                    UsageHistory(self.config.state_dir / "usage.json"), config=self.config,
                                    scheduler=self.scheduler)

    def store(self, bot_id: int) -> AlertStore:
        return AlertStore.for_bot(self.config, bot_id)

    def cycle(self, now: datetime = NOW, current: Optional[Reading] = None) -> Optional[Reading]:
        if current is not None:
            self.source.current = current
        return asyncio.run(self.monitor.cycle(now))

    def events(self, bot_id: int) -> List[Dict[str, Any]]:
        return [item for item in self.scheduler.list(99, bot_id=bot_id) if item["kind"] == KIND_EVENT]

    def test_nothing_to_check_means_no_reading(self) -> None:
        self.assertIsNone(self.cycle())
        self.assertEqual(self.source.reads, 0)
        self.assertFalse((self.config.state_dir / "usage.json").exists())

    def test_only_the_bot_that_set_it_is_woken_with_a_clear_text(self) -> None:
        alert = self.store(RESEARCH).add("session.left < 1h and session.used = 0", note="run the backlog", chat_id=99,
                                         thread_id=3)
        self.store(CHEF).add("week.used > 90", chat_id=99)  # does not hold
        self.assertIsNotNone(self.cycle())
        self.assertEqual(self.source.reads, 1)  # one reading for both bots
        (event,) = self.scheduler.list(99, bot_id=RESEARCH)
        self.assertEqual((event["kind"], event["bot_id"], event["thread_id"]), (KIND_EVENT, RESEARCH, 3))
        text = event["message"]
        for expected in ("Usage alert met", alert.id, "session.left < 1h and session.used = 0", "run the backlog",
                         "5-hour window: 0% used, resets", "Week (all models): 39% used", "Week (Fable): 59% used",
                         "will not fire again", "used some plan usage"):
            self.assertIn(expected, text)
        self.assertEqual(self.events(CHEF), [])
        self.assertTrue(self.store(RESEARCH).alerts()[0].spent)

    def test_fires_once_per_window_then_never_again(self) -> None:
        self.store(RESEARCH).add("session.used = 0", chat_id=99)
        self.store(CHEF).add("session.used = 0", repeat=True, chat_id=99)
        self.cycle()
        self.cycle(NOW + timedelta(minutes=15))
        self.assertEqual((len(self.events(RESEARCH)), len(self.events(CHEF))), (1, 1))
        later = NOW + timedelta(hours=5)
        self.cycle(later, reading(at=later))
        self.assertEqual((len(self.events(RESEARCH)), len(self.events(CHEF))), (1, 2))  # only the repeating one

    def test_one_wake_per_interval_and_held_alerts_together(self) -> None:
        self.store(RESEARCH).add("session.used = 0", repeat=True, chat_id=99)
        self.store(RESEARCH).add("week.used < 50", chat_id=99)
        self.cycle()
        (event,) = self.events(RESEARCH)  # both alerts in one wake-up
        self.assertIn("Usage alerts met", event["message"])
        self.monitor.settings.wake_interval_seconds = 6 * 3600
        soon = NOW + timedelta(hours=5)  # a new 5-hour window: the repeating alert holds again
        self.cycle(soon, reading(at=soon))
        self.assertEqual(len(self.events(RESEARCH)), 1)  # held: the bot was woken less than 6 hours ago
        self.assertEqual(len(self.store(RESEARCH).alerts()[0].fired_keys), 1)  # and not spent while held
        later = NOW + timedelta(hours=6, minutes=10)
        self.cycle(later, reading(at=soon))  # still that window, now past the interval
        self.assertEqual(len(self.events(RESEARCH)), 2)
        self.assertEqual(len(self.store(RESEARCH).alerts()[0].fired_keys), 2)

    def test_a_bot_without_the_permission_or_paused_is_not_checked(self) -> None:
        self.store(RESEARCH).add("session.used = 0", chat_id=99)
        record = self.registry.get(RESEARCH)
        record.permissions["usage"] = "off"
        self.registry.save(record)
        self.store(CHEF).add("session.used = 0", chat_id=99)
        chef = self.registry.get(CHEF)
        chef.enabled = False
        self.registry.save(chef)
        self.assertIsNone(self.cycle())
        self.assertEqual(self.source.reads, 0)
        self.assertEqual((self.events(RESEARCH), self.events(CHEF)), ([], []))
        self.assertFalse(self.store(RESEARCH).alerts()[0].spent)  # kept for when it is granted again

    def test_a_failed_reading_fires_nothing(self) -> None:
        self.store(RESEARCH).add("session.used = 0", chat_id=99)
        self.cycle(current=Reading.failed("rate limited until 3:14am", "text", NOW))
        self.assertEqual(self.events(RESEARCH), [])
        self.assertFalse(self.store(RESEARCH).alerts()[0].spent)

    def test_spare_weekly_estimate_uses_the_window_share(self) -> None:
        settings = MonitorSettings.from_config(self.config)
        self.assertEqual(settings.window_share_percent, 8.0)
        needed, available = SpareWeeklyRule.estimate(reading(week=60, week_left=timedelta(hours=20)), NOW, 8.0)
        self.assertEqual((needed, available), (5.0, 4.0))  # 40% unused takes 5 windows; 4 fit before the reset
        self.sandbox.environ["CONSIGLIERI_USAGE_WINDOW_SHARE"] = "12.5"
        self.assertEqual(MonitorSettings.from_config(self.config).window_share_percent, 12.5)
        self.assertEqual(SpareWeeklyRule().evaluate(None), [])  # the rule itself does nothing yet


class StructuredSourceTests(unittest.TestCase):
    def test_reads_the_usage_report_with_reset_times(self) -> None:
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        sandbox.home.mkdir()
        report = {"rate_limits": {"limits": [
            {"kind": "session", "group": "session", "percent": 0, "resets_at": "2026-11-12T10:50:00.12+00:00",
             "scope": None, "severity": "normal", "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": 39, "resets_at": "2026-11-15T05:00:00.1+00:00",
             "scope": None, "severity": "normal", "is_active": False},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 59, "resets_at": "2026-11-15T05:00:00.1+00:00",
             "scope": {"model": {"display_name": "Fable"}, "surface": None}, "severity": "warning", "is_active": True},
        ], "extra_usage": None}}
        sandbox.fake_cli("claude", {"fresh": [
            {"type": "system", "subtype": "init", "session_id": "s"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Current session: 0% used"}]},
             "usage_report": report},
            {"type": "result", "subtype": "success", "is_error": False, "result": "", "total_cost_usd": 0},
        ]})
        current = asyncio.run(ClaudeUsageSource(sandbox.config, dict(sandbox.environ)).read())
        self.assertEqual(current.source, "usage_report")
        self.assertEqual(current.window("session").resets_at, datetime(2026, 11, 12, 10, 50, tzinfo=timezone.utc))
        self.assertEqual((current.window("weekly_scoped").scope, current.window("weekly_scoped").severity), ("Fable", "warning"))
        (call,) = sandbox.calls("claude")
        self.assertEqual(call["stdin"], "/usage")
        for flag in ("stream-json", "--no-session-persistence", "--strict-mcp-config", "--setting-sources"):
            self.assertIn(flag, call["argv"])
        self.assertNotIn("--bare", call["argv"])
        self.assertTrue(check("session.left < 1h and session.used = 0", current))


class CommandTests(unittest.TestCase):
    """`alfred usage-alert` inside a bot's turn."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        home.prepare(self.sandbox.config)
        self.registry = BotRegistry(self.sandbox.config)
        for bot_id, username in ((RESEARCH, "research_bot"), (CHEF, "chef_bot")):
            self.registry.save(BotRecord(bot_id=bot_id, username=username, name=username, owner_id=99, chat_ids=[99],
                                         permissions={"usage": "on"}))
        self.sandbox.environ["CONSIGLIERI_USAGE_MONITOR"] = "1"

    def run_json(self, bot_id: int, *argv: str) -> Dict[str, Any]:
        environ = dict(self.sandbox.environ, CONSIGLIERI_BOT_ID=str(bot_id), CONSIGLIERI_CHAT_ID="99", CONSIGLIERI_USER_ID="99",
                       CONSIGLIERI_USERNAME="pablo", CONSIGLIERI_THREAD_ID="")
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, environ), redirect_stdout(buffer):
            cli.main(list(argv))
        return json.loads(buffer.getvalue())

    def test_add_list_remove_as_the_bot(self) -> None:
        added = self.run_json(RESEARCH, "usage-alert", "add", "--when", "session.left < 1h and session.used = 0",
                              "use", "the", "spare", "window")
        self.assertTrue(added["ok"], added)
        self.assertEqual(added["alert"]["note"], "use the spare window")
        self.assertIn("uses plan usage", added["cost"])
        listing = self.run_json(RESEARCH, "usage-alert", "list")
        self.assertEqual([item["id"] for item in listing["alerts"]], [added["alert"]["id"]])
        self.assertTrue(listing["monitor_on"])
        self.assertEqual(self.run_json(CHEF, "usage-alert", "list")["alerts"], [])  # each bot sees its own
        self.assertFalse(self.run_json(CHEF, "usage-alert", "remove", added["alert"]["id"])["ok"])
        self.assertFalse(self.run_json(CHEF, "usage-alert", "list", "--bot", "research_bot")["ok"])
        self.assertTrue(self.run_json(RESEARCH, "usage-alert", "remove", added["alert"]["id"][:5])["ok"])
        self.assertEqual(self.run_json(RESEARCH, "usage-alert", "list")["alerts"], [])

    def test_a_bad_condition_says_why(self) -> None:
        refused = self.run_json(RESEARCH, "usage-alert", "add", "--when", "session.used > lots")
        self.assertFalse(refused["ok"])
        self.assertIn("percentage", refused["error"])

    def test_refused_without_the_permission(self) -> None:
        record = self.registry.get(RESEARCH)
        record.permissions["usage"] = "off"
        self.registry.save(record)
        for argv in (("add", "--when", "week.used > 90"), ("list",), ("remove", "abc")):
            refused = self.run_json(RESEARCH, "usage-alert", *argv)
            self.assertFalse(refused["ok"])
            self.assertIn("usage permission", refused["error"])
        self.assertFalse(AlertStore.for_bot(self.sandbox.config, RESEARCH).path.exists())

    def test_refused_while_the_monitor_is_off(self) -> None:
        self.sandbox.environ["CONSIGLIERI_USAGE_MONITOR"] = "0"
        refused = self.run_json(RESEARCH, "usage-alert", "add", "--when", "week.used > 90")
        self.assertFalse(refused["ok"])
        self.assertIn("usage monitor is off", refused["error"])

    def test_only_its_own_chats(self) -> None:
        refused = self.run_json(RESEARCH, "usage-alert", "add", "--when", "week.used > 90", "--chat-id", "12345")
        self.assertFalse(refused["ok"])
        self.assertIn("owner's chats", refused["error"])


@unittest.skipIf(BotPool is None, "python-telegram-bot is not installed")
class PoolTests(unittest.TestCase):
    """The whole path with stand-in Telegram and Claude: the alert's event reaches that bot's agent only."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["CONSIGLIERI_ALLOWED_USERS"] = str(OWNER)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = token_for(1, "P")
        home.prepare(self.sandbox.config)
        self.sandbox.fake_cli("claude", {"fresh": [
            {"type": "system", "subtype": "init", "session_id": "sess"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Noted."}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Noted.", "session_id": "sess",
             "total_cost_usd": 0.01},
        ]})
        registry = BotRegistry(self.sandbox.config)
        for bot_id, username in ((RESEARCH, "research_bot"), (CHEF, "chef_bot")):
            registry.save(BotRecord(bot_id=bot_id, username=username, name=username, owner_id=OWNER,
                                    chat_ids=[OWNER], permissions={"usage": "on"}))
            registry.set_token(bot_id, token_for(bot_id))
            AlertStore.for_bot(self.sandbox.config, bot_id).add(
                "session.used = 0" if bot_id == RESEARCH else "week.used > 90", chat_id=OWNER)

    def agent_calls(self) -> List[str]:
        return [call["stdin"] for call in self.sandbox.calls("claude") if call.get("stdin") != "/usage"]

    def test_off_by_default_nothing_runs(self) -> None:
        async def run() -> None:
            pool = TestPool(self.sandbox.config)
            await pool.start()
            self.assertIsNone(pool.usage_monitor)
            await pool.stop()

        asyncio.run(run())
        self.assertEqual(self.sandbox.calls("claude"), [])

    def test_the_event_wakes_only_the_bot_that_set_the_alert(self) -> None:
        self.sandbox.environ["CONSIGLIERI_USAGE_MONITOR"] = "1"

        async def run() -> None:
            pool = TestPool(self.sandbox.config)
            await pool.start()
            try:
                monitor = pool.usage_monitor
                self.assertIsInstance(monitor, UsageMonitor)
                monitor.source = FixedSource(reading(at=datetime.now(timezone.utc).replace(microsecond=0)))
                await monitor.cycle()
                research, chef = pool.frontends[RESEARCH], pool.frontends[CHEF]
                await asyncio.wait_for(self._delivered(research), timeout=20)  # by the pool's own scheduler
                await research.agent.wait_idle(OWNER)
                self.assertEqual(research.bot.texts, ["Noted."])
                self.assertEqual(chef.bot.texts, [])
            finally:
                await pool.stop()

        asyncio.run(run())
        (prompt,) = self.agent_calls()
        self.assertIn("[Event] Usage alert met", prompt)
        self.assertIn("session.used = 0", prompt)

    async def _delivered(self, frontend: Any) -> None:
        while not frontend.bot.texts:
            await asyncio.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
