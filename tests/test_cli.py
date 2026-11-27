from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from consiglieri import cli
from consiglieri.text import split_message
from fakes import Sandbox, claude_lines


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        # As inside a bot's turn: every agent is a bot, and the commands act as it.
        self.record, self.bot_config = self.sandbox.add_bot()
        environ = dict(self.sandbox.environ, CONSIGLIERI_CHAT_ID="7", CONSIGLIERI_USER_ID="99", CONSIGLIERI_USERNAME="pablo",
                       CONSIGLIERI_BOT_ID=str(self.record.bot_id))
        patcher = mock.patch.dict(os.environ, environ)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv: str):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def run_json(self, *argv: str):
        code, output = self.run_cli(*argv)
        return code, json.loads(output)

    def test_schedule_add_list_cancel_round_trip(self) -> None:
        code, added = self.run_json("schedule", "add", "--in", "20m", "--message", "Take the pizza out")
        self.assertEqual((code, added["ok"]), (0, True))
        self.assertEqual((added["item"]["kind"], added["item"]["text"]), ("message", "Take the pizza out"))

        code, task = self.run_json(
            "schedule", "add", "--at", "2099-01-01T08:00:00Z", "--every", "1d", "--task", "Morning briefing"
        )
        self.assertEqual((task["item"]["kind"], task["item"]["repeat_seconds"]), ("task", 86400))
        self.assertEqual(task["item"]["due_utc"], "2099-01-01T08:00:00Z")

        _, listing = self.run_json("schedule", "list")
        self.assertEqual([item["id"] for item in listing["items"]], [added["item"]["id"], task["item"]["id"]])
        _, other_chat = self.run_json("schedule", "list", "--chat-id", "8")
        self.assertEqual(other_chat["items"], [])

        code, cancelled = self.run_json("schedule", "cancel", added["item"]["id"][:8])
        self.assertEqual((code, cancelled["item"]["status"]), (0, "cancelled"))
        _, listing = self.run_json("schedule", "list")
        self.assertEqual(len(listing["items"]), 1)
        _, everything = self.run_json("schedule", "list", "--all")
        self.assertEqual(len(everything["items"]), 2)

    def test_chrome_opens_alfreds_own_profile(self) -> None:
        code, refused = self.run_json("chrome")
        self.assertEqual((code, refused["ok"]), (1, False))
        self.assertIn("CONSIGLIERI_CHROME_PROFILE", refused["error"])

        os.environ["CONSIGLIERI_CHROME_PROFILE"] = "Alfred"
        with mock.patch("consiglieri.cli.subprocess.Popen") as popen, mock.patch("consiglieri.cli.sys.platform", "darwin"):
            code, opened = self.run_json("chrome")
        self.assertEqual((code, opened["profile"]), (0, "Alfred"))
        self.assertEqual(popen.call_args[0][0], ["open", "-na", "Google Chrome", "--args", "--profile-directory=Alfred"])

        with mock.patch("consiglieri.cli.sys.platform", "linux"):
            self.sandbox.environ.update(CONSIGLIERI_CHROME_PROFILE="Alfred", CONSIGLIERI_CHROME_APP="chromium")
            self.assertEqual(cli.chrome_command(self.sandbox.config), ["chromium", "--profile-directory=Alfred"])

    def test_event_is_queued_for_the_chat_and_due_at_once(self) -> None:
        from consiglieri.scheduler import SchedulerStore

        code, queued = self.run_json("event", "The", "agent in mus-solver", "finished")
        self.assertEqual((code, queued["ok"]), (0, True))
        self.assertEqual((queued["item"]["kind"], queued["item"]["text"]), ("event", "The agent in mus-solver finished"))

        # Another process on this machine names the bot and the chat itself.
        with mock.patch.dict(os.environ, {"CONSIGLIERI_BOT_ID": "", "CONSIGLIERI_CHAT_ID": ""}):
            code, other = self.run_json("event", "--bot", "helper_bot", "--chat-id", "8", "Backup done")
        self.assertEqual((code, other["ok"]), (0, True))
        store = SchedulerStore(self.sandbox.config.db_path)
        due = [store.claim_due(), store.claim_due()]
        self.assertEqual([(item["chat_id"], item["message"]) for item in due], [(7, "The agent in mus-solver finished"), (8, "Backup done")])
        self.assertEqual({item["bot_id"] for item in due}, {self.record.bot_id})
        self.assertEqual(due[0]["username"], "pablo")

        code, empty = self.run_json("event")
        self.assertEqual((code, empty["ok"]), (1, False))
        self.assertIn("nothing to report", empty["error"])

    def test_schedule_add_rejects_bad_input_with_json_errors(self) -> None:
        cases = [
            (["--message", "x"], "--at or --in"),
            (["--in", "5m", "--at", "2099-01-01T00:00:00Z", "--message", "x"], "--at or --in"),
            (["--in", "5m"], "--message or --task"),
            (["--in", "5m", "--message", "x", "--task", "y"], "--message or --task"),
            (["--at", "2001-01-01T00:00:00Z", "--message", "x"], "in the past"),
            (["--at", "next tuesday", "--message", "x"], "Invalid isoformat"),
            (["--in", "soon", "--message", "x"], "duration must be"),
            (["--in", "5m", "--every", "10s", "--message", "x"], "at least 60"),
        ]
        for arguments, expected in cases:
            code, payload = self.run_json("schedule", "add", *arguments)
            self.assertEqual((code, payload["ok"]), (1, False), arguments)
            self.assertIn(expected, payload["error"], arguments)
        code, payload = self.run_json("schedule", "cancel", "nope")
        self.assertEqual((code, payload["ok"]), (1, False))

    def test_agent_commands_need_a_chat(self) -> None:
        with mock.patch.dict(os.environ, {"CONSIGLIERI_CHAT_ID": ""}):
            code, payload = self.run_json("schedule", "list")
        self.assertEqual(code, 1)
        self.assertIn("no chat", payload["error"])

    def test_send_validates_before_touching_telegram(self) -> None:
        _, payload = self.run_json("send")
        self.assertIn("nothing to send", payload["error"])
        _, payload = self.run_json("send", "--file", str(self.sandbox.root / "missing.pdf"))
        self.assertIn("file not found", payload["error"])

    def test_init_then_skills_then_chat_with_a_bot(self) -> None:
        code, output = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn(str(self.sandbox.home), output)

        _, payload = self.run_json("skills")
        self.assertEqual([skill["name"] for skill in payload["skills"]], ["alfred:bot-admin", "alfred:browser", "alfred:calendar", "alfred:extensions", "alfred:orca-agents", "alfred:reminders", "alfred:usage", "alfred:web-logins"])

        self.sandbox.fake_cli("claude", {"fresh": claude_lines("At your service.", "sess-A")})
        with mock.patch.dict(os.environ, self.sandbox.environ):
            code, output = self.run_cli("chat", "--bot", "helper_bot", "-m", "hello")
        self.assertEqual((code, output), (0, "At your service.\n"))
        self.assertIn('source="terminal" chat_id="0"', self.sandbox.calls("claude")[0]["stdin"])

    def test_without_a_bot_there_is_no_agent_to_act_as(self) -> None:
        # From a terminal, outside any bot's turn: there is no main agent to post, schedule, wake or chat as.
        with mock.patch.dict(os.environ, {"CONSIGLIERI_BOT_ID": ""}):
            for argv in (("schedule", "add", "--in", "20m", "--message", "x"), ("schedule", "list"),
                         ("event", "hello"), ("send", "hello"), ("skills",), ("ext", "list")):
                code, payload = self.run_json(*argv)
                self.assertEqual((code, payload["ok"]), (1, False), argv)
                self.assertIn("main agent", payload["error"], argv)
            buffer = io.StringIO()
            with redirect_stdout(buffer), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["chat", "-m", "hello"])  # --bot is required
            for alias in ("main", "alfred", "0"):
                code, payload = self.run_json("event", "--bot", alias, "hello")
                self.assertIn("no bot called", payload["error"], alias)

    def test_no_command_prints_help(self) -> None:
        code, output = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage: alfred", output)


class SplitMessageTests(unittest.TestCase):
    def test_short_and_empty(self) -> None:
        self.assertEqual(split_message("  hi  "), ["hi"])
        self.assertEqual(split_message("   "), [])

    def test_prefers_paragraph_then_line_then_word_boundaries(self) -> None:
        text = ("a" * 60 + "\n\n" + "b" * 60 + "\n" + "c" * 60 + " " + "d" * 60)
        chunks = split_message(text, limit=100)
        self.assertEqual(chunks, ["a" * 60, "b" * 60, "c" * 60, "d" * 60])

    def test_hard_split_when_there_is_no_boundary(self) -> None:
        chunks = split_message("x" * 250, limit=100)
        self.assertEqual([len(chunk) for chunk in chunks], [100, 100, 50])
        self.assertEqual("".join(chunks), "x" * 250)


if __name__ == "__main__":
    unittest.main()
