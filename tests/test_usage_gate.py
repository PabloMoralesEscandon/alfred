"""Usage conditions on scheduled items, checked just before each delivery.

- The decision: `skip_if` holds a delivery back, `only_if` lets it through only when it holds, forecast values
  with active hours, and what happens when the usage can't be read or the bot lost the usage permission.
- `alfred schedule add --skip-if-low|--skip-if|--only-if|--only-if-waste`: validation, the usage permission,
  and what `schedule list` shows.
- The service: a held-back item never reaches the bot.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple
from unittest import mock

from consiglieri import cli, home, scheduler, usage_gate
from consiglieri.bots import BotRecord, BotRegistry
from consiglieri.scheduler import SchedulerStore
from consiglieri.usage_gate import Decision, Gate, GateError
from consiglieri.usage_monitor import Reading
from fakes import Sandbox
from test_usage_alerts import FixedSource
from test_usage_questions import NOW, reading

BOT = 5000000001


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config

    def decide(self, gate: Gate, current: Reading, allowed: bool = True) -> Decision:
        return usage_gate.check(self.config, gate.validate().to_text(), allowed, FixedSource(current), NOW)

    def test_skip_if_low_holds_back_when_a_limit_is_at_the_threshold(self) -> None:
        gate = Gate(skip_if="session.used >= 90 or week.used >= 90")
        held = self.decide(gate, reading(week=90, week_left=timedelta(days=3)))
        self.assertEqual((held.deliver, held.unread), (False, False))
        self.assertEqual(held.reason, "week 90% used (resets in 3d 0h)")
        self.forget()
        self.assertTrue(self.decide(gate, reading(week=89, session=95, session_left=timedelta(hours=1))).deliver is False)
        self.forget()
        passed = self.decide(gate, reading(week=60, session=20, session_left=timedelta(hours=1)))
        self.assertEqual((passed.deliver, passed.unread), (True, False))

    def test_only_if_lets_it_through_only_when_weekly_quota_would_be_lost(self) -> None:
        # 6 useful windows of 8% in the 30 hours left, with hours 0-24: 48% can be spent.
        gate = Gate(only_if="week.surplus >= 0 and week.used < 100")
        self.assertTrue(self.decide(gate, reading(week=40)).deliver)  # 60% left: 12 points will be lost
        self.forget()
        held = self.decide(gate, reading(week=60))  # 40% left: it can all still be used
        self.assertFalse(held.deliver)
        self.assertTrue(held.reason.startswith("only when week.surplus >= 0 and week.used < 100, and now "), held.reason)

    def test_the_active_hours_change_the_forecast(self) -> None:
        gate = Gate(only_if="week.surplus > 0", hours="9-12")  # 10:00 Thursday, 30 hours left: 2 h + 3 h of work
        self.assertTrue(self.decide(gate, reading(week=60)).deliver)  # 40% left, about 8% can be spent
        self.forget()
        self.assertFalse(self.decide(Gate(only_if="week.surplus > 0"), reading(week=60)).deliver)

    def test_when_the_usage_cant_be_read_skip_if_delivers_and_only_if_holds_back(self) -> None:
        failed = Reading.failed("rate limited", "usage_report", NOW)
        run = self.decide(Gate(skip_if="week.used >= 90"), failed)
        self.assertEqual((run.deliver, run.unread), (True, True))
        self.assertIn("rate limited", run.reason)
        self.forget()
        held = self.decide(Gate(only_if="week.surplus > 0"), failed)
        self.assertEqual((held.deliver, held.unread), (False, True))
        self.forget()
        self.assertFalse(self.decide(Gate(skip_if="week.used >= 90", if_unread="skip"), failed).deliver)

    def test_without_the_usage_permission_it_counts_as_unread_and_reads_nothing(self) -> None:
        source = FixedSource(reading(week=99))
        text = Gate(skip_if="week.used >= 90").to_text()
        decision = usage_gate.check(self.config, text, False, source, NOW)
        self.assertEqual((decision.deliver, decision.unread, source.reads), (True, True, 0))
        self.assertIn("no longer has the usage permission", decision.reason)

    def test_a_reading_minutes_old_is_reused(self) -> None:
        source = FixedSource(reading(week=95))
        gate = Gate(skip_if="week.used >= 90").to_text()
        for minutes in (0, 2, 4):
            usage_gate.check(self.config, gate, True, source, NOW + timedelta(minutes=minutes))
        self.assertEqual(source.reads, 1)

    def test_validation_and_storage(self) -> None:
        for bad in (Gate(), Gate(skip_if="week.used >"), Gate(only_if="fable.used > 3"),
                    Gate(skip_if="week.used > 1", hours="25-3"), Gate(skip_if="week.used > 1", if_unread="maybe")):
            with self.assertRaises(GateError):
                bad.validate()
        gate = Gate(skip_if="week.used >= 90", label="--skip-if-low week")
        self.assertEqual(Gate.from_text(gate.to_text()), gate)
        self.assertIsNone(Gate.from_text(None))
        self.assertEqual(gate.describe(), {"skip_if": "week.used >= 90", "label": "--skip-if-low week", "if_unread": "run"})
        self.assertEqual(Gate(only_if="week.surplus > 0").describe()["if_unread"], "skip")
        # Unreadable on the item: the item is delivered, as if the usage could not be read.
        decision = usage_gate.check(self.config, "{not json", True, FixedSource(reading()), NOW)
        self.assertEqual((decision.deliver, decision.unread), (True, True))

    def forget(self) -> None:
        for path in self.config.state_dir.glob("usage.json*"):
            path.unlink()


class ScheduleCommandTests(unittest.TestCase):
    """`alfred schedule add` with usage conditions, from inside a bot's turn."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        home.prepare(self.sandbox.config)
        self.registry = BotRegistry(self.sandbox.config)
        self.grant(True)

    def grant(self, usage: bool) -> None:
        self.registry.save(BotRecord(bot_id=BOT, username="research_bot", name="Research", owner_id=99, chat_ids=[99],
                                     permissions={"usage": "on" if usage else "off"}))

    def add(self, *argv: str, extra: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any], str]:
        environ = dict(self.sandbox.environ, ALFRED_BOT_ID=str(BOT), ALFRED_CHAT_ID="99", **(extra or {}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["schedule", "add", "--in", "1h", "--task", "write the report"] + list(argv))
        return code, json.loads(out.getvalue() or "{}"), err.getvalue()

    def stored(self, payload: Dict[str, Any]) -> Gate:
        item = SchedulerStore(self.sandbox.config.db_path).get(payload["item"]["id"])
        return Gate.from_text(item["gate"])

    def test_skip_if_low_with_its_options(self) -> None:
        code, payload, _ = self.add("--skip-if-low")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.stored(payload).skip_if, "session.used >= 90 or week.used >= 90")
        self.assertEqual(payload["item"]["usage"]["label"], "--skip-if-low any")
        self.assertIn("not delivered", payload["usage_note"])

        _, payload, _ = self.add("--skip-if-low", "week", "--threshold", "80", "--projected")
        self.assertEqual(self.stored(payload).skip_if, "week.used >= 80 or week.projected >= 100")
        _, payload, _ = self.add("--skip-if-low", "session", extra={"ALFRED_USAGE_LOW_THRESHOLD": "75"})
        self.assertEqual(self.stored(payload).skip_if, "session.used >= 75")

    def test_skip_if_only_if_waste_hours_and_unread_policy(self) -> None:
        _, payload, _ = self.add("--skip-if", "week.left < 1d and week.used > 80", "--if-unread", "skip")
        gate = self.stored(payload)
        self.assertEqual((gate.skip_if, gate.unread_policy), ("week.left < 1d and week.used > 80", "skip"))

        code, payload, _ = self.add("--only-if-waste", "--margin", "5", "--hours", "9-20", "--days", "mon-fri",
                                    "--skip-if-low", "session")
        self.assertEqual(code, 0, payload)
        gate = self.stored(payload)
        self.assertEqual(gate.only_if, "week.surplus >= 5 and week.used < 100")
        self.assertEqual((gate.skip_if, gate.hours, gate.unread_policy), ("session.used >= 90", "09:00-20:00 mon-fri", "skip"))
        listed = self.list_items()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[-1]["usage"]["only_if"], "week.surplus >= 5 and week.used < 100")

    def test_mistakes_are_refused_and_nothing_is_scheduled(self) -> None:
        for argv in (("--skip-if", "week.used >"), ("--only-if", "money > 5"), ("--threshold", "80"),
                     ("--skip-if-low", "--threshold", "120"), ("--skip-if-low", "--hours", "26-3"),
                     ("--hours", "9-20"), ("--margin", "3"), ("--if-unread", "skip"),
                     ("--skip-if-low", "--skip-if", "week.used > 50")):
            code, payload, err = self.add(*argv)
            self.assertNotEqual(code, 0, argv)
            self.assertFalse(payload.get("ok"), argv)
        self.assertEqual(self.list_items(), [])

    def test_a_bot_without_the_usage_permission_cant_add_conditions_but_still_schedules(self) -> None:
        self.grant(False)
        code, payload, _ = self.add("--skip-if-low")
        self.assertNotEqual(code, 0)
        self.assertIn("usage permission", payload["error"])
        self.assertEqual(self.list_items(), [])
        code, payload, _ = self.add()
        self.assertEqual(code, 0)
        self.assertNotIn("usage", payload["item"])

    def test_the_monitor_switch_is_not_needed(self) -> None:
        code, payload, _ = self.add("--skip-if-low", extra={"ALFRED_USAGE_MONITOR": "0"})
        self.assertEqual(code, 0, payload)

    def list_items(self) -> list:
        environ = dict(self.sandbox.environ, ALFRED_BOT_ID=str(BOT), ALFRED_CHAT_ID="99")
        out = io.StringIO()
        with mock.patch.dict(os.environ, environ), redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["schedule", "list"])
        return json.loads(out.getvalue())["items"]
