"""The `alfred` command inside a bot's turn: it acts as that bot, in its chats, within its permissions."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List
from unittest import mock

from alfred import cli, home
from alfred.bots import BotRecord, BotRegistry
from alfred.scheduler import SchedulerStore
from fakes import Sandbox

RESEARCH = 5000000001
CHEF = 5000000002
TOKENS = {RESEARCH: "%d:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" % RESEARCH, CHEF: "%d:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC" % CHEF}


class FakeTelegramBot:
    """Stands in for telegram.Bot: records which token posted what, and where."""

    sent: List[Dict[str, Any]] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.username = "AlfredPanelBot"

    async def __aenter__(self) -> "FakeTelegramBot":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def send_message(self, **kwargs: Any) -> None:
        FakeTelegramBot.sent.append(dict(kwargs, token=self.token, kind="message"))

    async def send_document(self, **kwargs: Any) -> None:
        kwargs.pop("document", None)
        FakeTelegramBot.sent.append(dict(kwargs, token=self.token, kind="document"))

    async def send_photo(self, **kwargs: Any) -> None:
        kwargs.pop("photo", None)
        FakeTelegramBot.sent.append(dict(kwargs, token=self.token, kind="photo"))


class BotCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        home.bootstrap(self.sandbox.config, legacy_dir=None)
        self.registry = BotRegistry(self.sandbox.config)
        for bot_id, username in ((RESEARCH, "research_bot"), (CHEF, "chef_bot")):
            self.registry.save(BotRecord(bot_id=bot_id, username=username, name=username[:-4], owner_id=99, chat_ids=[99]))
            self.registry.set_token(bot_id, TOKENS[bot_id])
        self.as_bot(RESEARCH)
        FakeTelegramBot.sent = []
        patcher = mock.patch("telegram.Bot", FakeTelegramBot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def as_bot(self, bot_id: int, thread: str = "") -> None:
        environ = dict(self.sandbox.environ, ALFRED_BOT_ID=str(bot_id), ALFRED_CHAT_ID="99", ALFRED_USER_ID="99",
                       ALFRED_USERNAME="pablo", ALFRED_THREAD_ID=thread)
        patcher = mock.patch.dict(os.environ, environ)
        patcher.start()
        self.addCleanup(patcher.stop)

    def grant(self, bot_id: int, **permissions: str) -> None:
        record = self.registry.get(bot_id)
        record.permissions.update(permissions)
        self.registry.save(record)

    def run_json(self, *argv: str) -> Dict[str, Any]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli.main(list(argv))
        return json.loads(buffer.getvalue())

    def test_schedule_records_the_bot_and_topic_and_each_bot_sees_only_its_own(self) -> None:
        self.as_bot(RESEARCH, thread="3")
        added = self.run_json("schedule", "add", "--in", "20m", "--message", "Check arXiv")
        self.assertTrue(added["ok"], added)
        item = SchedulerStore(self.sandbox.config.db_path).get(added["item"]["id"])
        self.assertEqual((item["bot_id"], item["chat_id"], item["thread_id"]), (RESEARCH, 99, 3))

        self.assertEqual(len(self.run_json("schedule", "list")["items"]), 1)
        self.as_bot(CHEF)
        self.assertEqual(self.run_json("schedule", "list")["items"], [])
        refused = self.run_json("schedule", "cancel", added["item"]["id"][:8])
        self.assertFalse(refused["ok"])  # another bot's item
        self.assertFalse(self.run_json("schedule", "list", "--bot", "research_bot")["ok"])

    def test_schedule_needs_the_schedule_permission(self) -> None:
        self.grant(RESEARCH, schedule="off")
        refused = self.run_json("schedule", "add", "--in", "20m", "--message", "x")
        self.assertFalse(refused["ok"])
        self.assertIn("schedule permission", refused["error"])

    def test_send_posts_as_the_bot_in_its_topic_and_only_to_its_owner(self) -> None:
        self.as_bot(RESEARCH, thread="3")
        self.assertTrue(self.run_json("send", "Halfway there")["ok"])
        self.assertEqual(FakeTelegramBot.sent[-1]["token"], TOKENS[RESEARCH])
        self.assertEqual((FakeTelegramBot.sent[-1]["chat_id"], FakeTelegramBot.sent[-1]["message_thread_id"]), (99, 3))

        refused = self.run_json("send", "--chat-id", "12345", "hello stranger")
        self.assertFalse(refused["ok"])
        self.assertIn("contact", refused["error"])
        self.assertFalse(self.run_json("send", "--bot", "chef_bot", "as someone else")["ok"])

        self.grant(RESEARCH, contact="on")
        self.assertTrue(self.run_json("send", "--chat-id", "12345", "hello")["ok"])
        self.assertNotIn("message_thread_id", FakeTelegramBot.sent[-1])  # another chat: not this turn's topic

    def test_send_file_only_from_the_bots_own_directories(self) -> None:
        workspace = self.registry.workspace(RESEARCH)
        (workspace / "outbox").mkdir(parents=True)
        report = workspace / "outbox" / "report.pdf"
        report.write_bytes(b"%PDF")
        self.assertTrue(self.run_json("send", "--file", str(report), "Here")["ok"])
        secret = self.sandbox.root / "secret.txt"
        secret.write_text("x")
        refused = self.run_json("send", "--file", str(secret), "leak")
        self.assertFalse(refused["ok"])
        self.assertIn("own directories", refused["error"])

    def test_event_wakes_only_this_bot(self) -> None:
        queued = self.run_json("event", "build finished")
        item = SchedulerStore(self.sandbox.config.db_path).get(queued["item"]["id"])
        self.assertEqual(item["bot_id"], RESEARCH)

    def test_bots_commands_need_the_bots_permission_and_never_reach_permissions(self) -> None:
        self.assertFalse(self.run_json("bots", "list")["ok"])
        self.grant(RESEARCH, bots="on")
        listing = self.run_json("bots", "list")
        self.assertEqual([bot["username"] for bot in listing["bots"]], ["research_bot", "chef_bot"])
        self.assertFalse(self.run_json("bots", "set", "research_bot", "model", "opus")["ok"])  # itself
        self.assertFalse(self.run_json("bots", "set", "chef_bot", "workdir", str(self.sandbox.root))["ok"])
        self.assertFalse(self.run_json("bots", "set", "chef_bot", "web", "on")["ok"])  # a permission
        changed = self.run_json("bots", "set", "chef_bot", "model", "opus")
        self.assertTrue(changed["ok"], changed)
        self.assertEqual(self.registry.get(CHEF).settings["model"], "opus")

    def test_bots_new_asks_the_owner_through_the_control_panel(self) -> None:
        self.grant(RESEARCH, bots="on")
        asked = self.run_json("bots", "new", "Helper", "Fetch", "the", "data")
        self.assertTrue(asked["ok"], asked)
        message = FakeTelegramBot.sent[-1]
        self.assertEqual((message["token"], message["chat_id"]), ("1:PANELPANELPANELPANELPANELPANELPANEL", 99))
        self.assertIn("@research_bot asks for it", message["text"])
        self.assertEqual(asked["link"], "https://t.me/newbot/AlfredPanelBot/helper_bot?name=Helper")
        pending = self.registry.take_pending(99, "helper_bot")
        self.assertEqual((pending.settings, pending.permissions), ({"task": "Fetch the data"}, {}))

    def test_skills_and_extensions_are_the_bots_own(self) -> None:
        created = self.run_json("ext", "new", "papers", "Reading", "papers")
        self.assertTrue(created["ok"], created)
        self.assertTrue((self.registry.workspace(RESEARCH) / "extensions" / "papers").is_dir())
        self.assertFalse((self.sandbox.config.extensions_dir / "papers").exists())
        self.as_bot(CHEF)
        self.assertEqual([extension["name"] for extension in self.run_json("ext", "list")["extensions"]], [])


if __name__ == "__main__":
    unittest.main()
