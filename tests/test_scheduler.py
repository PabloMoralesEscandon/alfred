from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import support_telegram

support_telegram.install()

import scheduler_store
import scheduler_tool
import scheduler_worker


TEST_USERNAME = "allowed_user"


class SchedulerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        handle.close()
        self.db_path = Path(handle.name)
        self.addCleanup(self.db_path.unlink, missing_ok=True)

    def test_create_cancel_and_due_claim(self) -> None:
        scheduled = scheduler_store.create_message(
            chat_id=123,
            user_id=456,
            username=TEST_USERNAME,
            due_at_utc="2020-01-01T00:00:00Z",
            message="Call Sam",
            source_request="Remind me",
            db_path=self.db_path,
        )
        cancelled = scheduler_store.create_message(
            chat_id=123,
            user_id=456,
            username=TEST_USERNAME,
            due_at_utc="2020-01-01T00:00:01Z",
            message="Ignore me",
            source_request="Cancel this",
            db_path=self.db_path,
        )
        scheduler_store.cancel_message(cancelled["id"], 123, db_path=self.db_path)

        claimed = scheduler_store.claim_due_message(
            "2020-01-01T00:01:00Z",
            db_path=self.db_path,
        )

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], scheduled["id"])
        self.assertEqual(claimed["status"], scheduler_store.STATUS_DELIVERING)
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNone(
            scheduler_store.claim_due_message("2020-01-01T00:01:00Z", db_path=self.db_path)
        )


class SchedulerToolTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        handle.close()
        self.db_path = Path(handle.name)
        self.addCleanup(self.db_path.unlink, missing_ok=True)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_env_path = scheduler_tool.ENV_PATH
        scheduler_tool.ENV_PATH = Path(self.tempdir.name) / ".env"
        scheduler_tool.ENV_PATH.write_text(
            f"whitelisted@{TEST_USERNAME}\n",
            encoding="utf-8",
        )
        self.addCleanup(setattr, scheduler_tool, "ENV_PATH", self.original_env_path)

    def run_tool(self, *args: str) -> tuple[int, dict[str, object]]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = scheduler_tool.main(["--db-path", str(self.db_path), *args])
        payload = scheduler_tool.json.loads(stream.getvalue())
        return code, payload

    def test_add_list_show_cancel(self) -> None:
        code, added = self.run_tool(
            "add",
            "--chat-id",
            "123",
            "--user-id",
            "456",
            "--username",
            TEST_USERNAME,
            "--due",
            "2099-01-01T00:00:00Z",
            "--message",
            "Call Sam",
            "--source-request",
            "Remind me to call Sam",
        )
        self.assertEqual(code, 0)
        self.assertTrue(added["ok"])

        code, listed = self.run_tool("list", "--chat-id", "123")
        self.assertEqual(code, 0)
        self.assertEqual(len(listed["items"]), 1)

        message_id = str(added["id"])
        code, shown = self.run_tool("show", "--id", message_id)
        self.assertEqual(code, 0)
        self.assertEqual(shown["item"]["id"], message_id)

        code, cancelled = self.run_tool("cancel", "--id", message_id, "--chat-id", "123")
        self.assertEqual(code, 0)
        self.assertEqual(cancelled["item"]["status"], scheduler_store.STATUS_CANCELLED)

    def test_validation_errors_are_json(self) -> None:
        code, payload = self.run_tool(
            "add",
            "--chat-id",
            "123",
            "--user-id",
            "456",
            "--username",
            "someone_else",
            "--due",
            "2099-01-01T00:00:00Z",
            "--message",
            "Call Sam",
            "--source-request",
            "Remind me",
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["error"], "username is not allowed")


class SchedulerWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        handle.close()
        self.db_path = Path(handle.name)
        self.addCleanup(self.db_path.unlink, missing_ok=True)

    def test_once_worker_marks_sent_with_mocked_delivery(self) -> None:
        scheduler_store.create_message(
            chat_id=123,
            user_id=456,
            username=TEST_USERNAME,
            due_at_utc="2020-01-01T00:00:00Z",
            message="Call Sam",
            source_request="Remind me",
            db_path=self.db_path,
        )
        delivered: list[tuple[int, str]] = []
        original = scheduler_worker.bot.send_reminder

        async def fake_send_reminder(chat_id: int, message: str) -> None:
            delivered.append((chat_id, message))

        scheduler_worker.bot.send_reminder = fake_send_reminder
        self.addCleanup(setattr, scheduler_worker.bot, "send_reminder", original)

        asyncio.run(
            scheduler_worker.run_worker(db_path=self.db_path, once=True, poll_seconds=1)
        )

        rows = scheduler_store.list_messages(123, db_path=self.db_path)
        self.assertEqual(delivered, [(123, "Call Sam")])
        self.assertEqual(rows[0]["status"], scheduler_store.STATUS_SENT)
        self.assertEqual(rows[0]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
