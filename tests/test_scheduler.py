from __future__ import annotations

import asyncio
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from consiglieri import scheduler
from consiglieri.scheduler import SchedulerStore
from fakes import Sandbox

T0 = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.store = SchedulerStore(self.sandbox.root / "state" / "scheduler.sqlite3")

    def test_parse_helpers(self) -> None:
        self.assertEqual(scheduler.parse_duration("90s"), 90)
        self.assertEqual(scheduler.parse_duration(" 2H "), 7200)
        self.assertEqual(scheduler.parse_duration("1w"), 604800)
        for bad in ("", "10", "m", "1.5h", "0m", "5 minutes"):
            with self.assertRaises(ValueError, msg=bad):
                scheduler.parse_duration(bad)
        self.assertEqual(scheduler.to_utc_text("2030-01-01T10:00:00+01:00"), "2030-01-01T09:00:00Z")
        self.assertEqual(scheduler.to_utc_text("2030-01-01T09:00:00Z"), "2030-01-01T09:00:00Z")
        local = datetime(2030, 1, 1, 9, 0).astimezone()
        self.assertEqual(scheduler.parse_time("2030-01-01T09:00"), local.astimezone(timezone.utc))

    def test_one_off_is_claimed_once_in_due_order_and_marked_sent(self) -> None:
        later = self.store.add(chat_id=1, due=T0 + timedelta(minutes=5), text="later")
        first = self.store.add(chat_id=1, due=T0, text="first")
        self.assertIsNone(self.store.claim_due(T0 - timedelta(seconds=1)))

        claimed = self.store.claim_due(T0)
        self.assertEqual(claimed["id"], first["id"])
        self.assertEqual(claimed["status"], scheduler.STATUS_DELIVERING)
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNone(self.store.claim_due(T0))

        self.assertEqual(self.store.finish(first["id"], now=T0)["status"], scheduler.STATUS_SENT)
        self.assertEqual([item["id"] for item in self.store.list(1)], [later["id"]])
        self.assertEqual(len(self.store.list(1, include_done=True)), 2)

    def test_failed_one_off_records_the_error(self) -> None:
        item = self.store.add(chat_id=1, due=T0, text="x")
        self.store.claim_due(T0)
        failed = self.store.finish(item["id"], error="telegram down", now=T0)
        self.assertEqual((failed["status"], failed["last_error"]), (scheduler.STATUS_FAILED, "telegram down"))

    def test_recurring_item_is_rescheduled_and_skips_missed_runs(self) -> None:
        item = self.store.add(chat_id=1, due=T0, text="standup", kind=scheduler.KIND_TASK, repeat_seconds=3600)
        self.store.claim_due(T0)
        again = self.store.finish(item["id"], now=T0 + timedelta(seconds=5))
        self.assertEqual((again["status"], again["due_at_utc"]), (scheduler.STATUS_SCHEDULED, "2030-01-01T10:00:00Z"))

        # The bot was down for a while: one catch-up run, then back on the grid, in the future.
        much_later = T0 + timedelta(hours=5, minutes=30)
        self.store.claim_due(much_later)
        again = self.store.finish(item["id"], error="boom", now=much_later)
        self.assertEqual(again["due_at_utc"], "2030-01-01T15:00:00Z")
        self.assertEqual((again["status"], again["last_error"]), (scheduler.STATUS_SCHEDULED, "boom"))

    def test_a_held_back_delivery_moves_a_recurring_item_on_and_ends_a_one_off_as_skipped(self) -> None:
        daily = self.store.add(chat_id=1, due=T0, text="report", kind=scheduler.KIND_TASK, repeat_seconds=86400,
                               gate='{"skip_if": "week.used >= 90"}')
        self.assertEqual(daily["gate"], '{"skip_if": "week.used >= 90"}')
        self.store.claim_due(T0)
        held = self.store.skip(daily["id"], "week 92% used", now=T0 + timedelta(seconds=3))
        self.assertEqual((held["status"], held["due_at_utc"]), (scheduler.STATUS_SCHEDULED, "2030-01-02T09:00:00Z"))
        self.assertEqual((held["last_skip"], held["skips"], held["delivered_at_utc"]),
                         ("2030-01-01T09:00:03Z week 92% used", 1, None))

        once = self.store.add(chat_id=1, due=T0, text="reminder")
        self.store.claim_due(T0)
        held = self.store.skip(once["id"], "5-hour window 95% used", now=T0)
        self.assertEqual((held["status"], held["skips"]), (scheduler.STATUS_SKIPPED, 1))
        self.assertEqual(self.store.list(1), [self.store.get(daily["id"])])  # done items only with include_done
        # Cancelled while its condition was being checked: it stays cancelled.
        self.store.claim_due(T0 + timedelta(days=1))
        self.store.cancel(daily["id"], chat_id=1)
        self.assertEqual(self.store.skip(daily["id"], "x")["status"], scheduler.STATUS_CANCELLED)

    def test_the_worker_records_a_skip_instead_of_a_failure(self) -> None:
        item = self.store.add(chat_id=1, due=scheduler.utc_now() - timedelta(minutes=1), text="report")

        async def deliver(item) -> None:
            raise scheduler.Skip("week 92% used")

        with self.assertLogs("consiglieri.scheduler", level="INFO"):
            asyncio.run(scheduler.run_scheduler(self.store, deliver, once=True))
        done = self.store.get(item["id"])
        self.assertEqual((done["status"], done["last_error"]), (scheduler.STATUS_SKIPPED, None))
        self.assertTrue(done["last_skip"].endswith(" week 92% used"))

    def test_cancel_by_prefix_is_scoped_to_the_chat_and_sticks_during_delivery(self) -> None:
        item = self.store.add(chat_id=1, due=T0, text="x", repeat_seconds=3600)
        self.assertIsNone(self.store.cancel(item["id"], chat_id=2))
        self.assertIsNone(self.store.cancel("", chat_id=1))
        self.store.claim_due(T0)
        self.assertEqual(self.store.cancel(item["id"][:8], chat_id=1)["status"], scheduler.STATUS_CANCELLED)
        self.assertEqual(self.store.finish(item["id"], now=T0)["status"], scheduler.STATUS_CANCELLED)
        self.assertIsNone(self.store.cancel(item["id"], chat_id=1))

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add(chat_id=1, due=T0, text="   ")
        with self.assertRaises(ValueError):
            self.store.add(chat_id=1, due=T0, text="x", repeat_seconds=5)
        with self.assertRaises(ValueError):
            self.store.add(chat_id=1, due=T0, text="x", kind="email")

    def test_database_from_the_first_version_is_migrated(self) -> None:
        self.store.db_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(str(self.store.db_path))
        connection.execute(
            """CREATE TABLE scheduled_messages (id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
               username TEXT NOT NULL, due_at_utc TEXT NOT NULL, message TEXT NOT NULL, source_request TEXT NOT NULL,
               status TEXT NOT NULL, created_at_utc TEXT NOT NULL, delivered_at_utc TEXT,
               attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)"""
        )
        connection.execute(
            "INSERT INTO scheduled_messages VALUES ('old-1', 1, 2, 'u', '2030-01-01T09:00:00Z', 'Call Sam', 'req', "
            "'scheduled', '2029-01-01T00:00:00Z', NULL, 0, NULL)"
        )
        connection.commit()
        connection.close()

        claimed = self.store.claim_due(T0)
        self.assertEqual((claimed["id"], claimed["kind"], claimed["repeat_seconds"]), ("old-1", "message", None))
        self.assertEqual((claimed["gate"], claimed["last_skip"], claimed["skips"]), (None, None, 0))

    def test_worker_delivers_due_items_survives_failures_and_requeues_interrupted_ones(self) -> None:
        past = scheduler.utc_now() - timedelta(minutes=1)
        good = self.store.add(chat_id=1, due=past, text="good")
        bad = self.store.add(chat_id=1, due=past, text="bad")
        future = self.store.add(chat_id=1, due=scheduler.utc_now() + timedelta(days=1), text="future")
        interrupted = self.store.add(chat_id=1, due=past - timedelta(minutes=1), text="interrupted")
        self.assertEqual(self.store.claim_due()["id"], interrupted["id"])  # a crash left it mid-delivery

        delivered = []

        async def deliver(item) -> None:
            if item["message"] == "bad":
                raise RuntimeError("nope")
            delivered.append(item["message"])

        with self.assertLogs("consiglieri.scheduler", level="ERROR"):
            asyncio.run(scheduler.run_scheduler(self.store, deliver, once=True))

        self.assertEqual(sorted(delivered), ["good", "interrupted"])
        self.assertEqual(self.store.get(good["id"])["status"], scheduler.STATUS_SENT)
        self.assertEqual(self.store.get(bad["id"])["status"], scheduler.STATUS_FAILED)
        self.assertEqual(self.store.get(bad["id"])["last_error"], "nope")
        self.assertEqual(self.store.get(future["id"])["status"], scheduler.STATUS_SCHEDULED)


if __name__ == "__main__":
    unittest.main()
