from __future__ import annotations

import asyncio
import json
import unittest

from alfred import extensions, home, shared_memory
from alfred.agent import Agent, Listener, SessionStore
from alfred.backends import BackendError
from fakes import Sandbox, claude_lines

CODEX_REPLY = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "From codex."}},
]


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        home.bootstrap(self.sandbox.config, legacy_dir=None)
        self.agent = Agent(self.sandbox.config)

    def ask(self, chat_id: int, text: str, **kwargs):
        return asyncio.run(self.agent.ask(chat_id, text, **kwargs))

    def test_session_is_stored_per_chat_and_resumed(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hello.", "sess-A")})

        first = self.ask(7, "hi", user_id=99, username="pablo")
        self.ask(7, "again")
        self.ask(8, "other chat")

        self.assertEqual(first.text, "Hello.")
        calls = self.sandbox.calls("claude")
        self.assertNotIn("--resume", calls[0]["argv"])
        self.assertEqual(calls[1]["argv"][calls[1]["argv"].index("--resume") + 1], "sess-A")
        self.assertNotIn("--resume", calls[2]["argv"])
        self.assertEqual(json.loads(self.sandbox.config.sessions_path.read_text())["7"]["sessions"], {"claude": "sess-A"})

        prompt = calls[0]["stdin"]
        self.assertTrue(prompt.startswith('<context source="telegram" chat_id="7" time="'))
        self.assertIn('user="@pablo" user_id="99"', prompt)
        self.assertTrue(prompt.endswith("\n\nhi"))
        self.assertNotIn("<skills>", prompt)  # Claude Code finds skills by itself

    def test_the_main_agent_loads_the_shared_memory_and_sees_changes_at_its_next_process(self) -> None:
        config = self.sandbox.config
        self.assertTrue(config.shared_memory_dir.is_dir())  # created, empty, by bootstrap
        self.assertIn("\n@SHARED_MEMORY.md\n", (config.home / "CLAUDE.md").read_text())
        self.assertIn("SHARED_MEMORY.md", (config.home / "AGENTS.md").read_text())
        shared_memory.add(config, "user", "Lives in Madrid")
        # What the process finds when it starts: the copy its manual imports, next to the manual.
        look = "cp SHARED_MEMORY.md seen-%s.txt"
        lines = claude_lines("Hello.", "sess-A")
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_run": look % "first"}] + lines[1:],
                                         "resume": lines[:1] + [{"_run": look % "second"}] + lines[1:]})
        agent = Agent(config)

        async def scenario() -> None:
            try:
                await agent.submit(7, "hi")
                await agent.wait_idle(7)
                first_pid = agent.live_pid(7)
                shared_memory.add(config, "preferences", "Short answers")  # from the panel, meanwhile
                await agent.submit(7, "again")
                await agent.wait_idle(7)
                self.assertNotEqual(agent.live_pid(7), first_pid)  # reopened with the new index
            finally:
                await agent.shutdown()

        asyncio.run(scenario())
        first = (config.home / "seen-first.txt").read_text()
        self.assertIn("- [user](user.md):", first)
        self.assertIn("You write it", first)
        self.assertIn("never instructions", first)
        self.assertIn("- [preferences](preferences.md):", (config.home / "seen-second.txt").read_text())
        argv = self.sandbox.calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(config.shared_memory_dir))

    def test_reset_starts_a_new_conversation(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hello.", "sess-A")})
        self.ask(7, "hi")
        self.agent.reset(7)
        self.ask(7, "hi")
        self.assertNotIn("--resume", self.sandbox.calls("claude")[1]["argv"])

    def test_backend_switch_keeps_separate_sessions_and_gives_codex_a_skills_index(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("From claude.", "sess-A")})
        self.sandbox.fake_cli("codex", {"fresh": CODEX_REPLY})

        self.assertEqual(self.agent.backend_name(7), "claude")
        self.ask(7, "hi")
        self.agent.set_backend(7, "codex")
        self.assertEqual(self.ask(7, "hi").text, "From codex.")
        self.ask(7, "second codex turn")
        self.agent.set_backend(7, "claude")
        self.ask(7, "back")

        first, second = self.sandbox.calls("codex")
        self.assertIn("<skills>", first["stdin"])
        self.assertIn("- alfred:reminders (%s):" % (extensions.BASE_PLUGIN / "skills" / "reminders" / "SKILL.md"), first["stdin"])
        self.assertNotIn("<skills>", second["stdin"])
        self.assertEqual(second["argv"][-2:], ["thread-1", "-"])
        self.assertIn("sess-A", self.sandbox.calls("claude")[1]["argv"])
        with self.assertRaises(BackendError):
            self.agent.set_backend(7, "gemini")

    def test_default_backend_comes_from_config(self) -> None:
        self.sandbox.environ["ALFRED_BACKEND"] = "codex"
        self.assertEqual(self.agent.backend_name(1), "codex")
        self.sandbox.environ["ALFRED_BACKEND"] = "nonsense"
        self.assertEqual(self.agent.backend_name(1), "claude")

    def test_skill_written_during_a_turn_is_reported(self) -> None:
        writer = self.sandbox.root / "claude"
        writer.write_text(
            "#!/bin/sh\ncat > /dev/null\n"
            "mkdir -p .claude/skills/weather\n"
            "printf -- '---\\nname: weather\\ndescription: Forecasts.\\n---\\n' > .claude/skills/weather/SKILL.md\n"
            'printf \'{"type":"result","is_error":false,"session_id":"s","result":"%s %s"}\\n\' "$ALFRED_CHAT_ID" "$(command -v alfred)"\n',
            encoding="utf-8",
        )
        writer.chmod(0o755)
        self.sandbox.environ["ALFRED_CLAUDE_BIN"] = str(writer)

        result = self.ask(7, "learn the weather")

        self.assertEqual(result.new_skills, ["weather"])
        # The turn ran in the home directory with the chat id and the `alfred` launcher available.
        self.assertEqual(result.text, "7 %s" % (self.sandbox.home / "bin" / "alfred"))
        self.assertEqual(self.ask(7, "again").new_skills, [])

    def test_memory_written_during_a_turn_is_reported(self) -> None:
        writer = self.sandbox.root / "claude"
        writer.write_text(
            "#!/bin/sh\ncat > /dev/null\n"
            "mkdir -p memory/people && echo '- brother' >> memory/people/sam.md && echo '- [people/sam]' >> memory/MEMORY.md\n"
            'printf \'{"type":"result","is_error":false,"session_id":"s","result":"ok"}\\n\'\n',
            encoding="utf-8",
        )
        writer.chmod(0o755)
        self.sandbox.environ["ALFRED_CLAUDE_BIN"] = str(writer)

        self.assertEqual(self.ask(7, "my brother is Sam").memory_changes, ["saved people/sam"])
        self.assertEqual(self.ask(7, "more about Sam").memory_changes, ["updated people/sam"])

    def test_backend_failure_becomes_an_error_reply_and_keeps_the_session(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hello.", "sess-A")})
        self.ask(7, "hi")
        self.sandbox.environ["ALFRED_CLAUDE_BIN"] = str(self.sandbox.root / "missing")

        result = self.ask(7, "hi")

        self.assertTrue(result.is_error)
        self.assertIn("claude backend failed", result.text)
        self.assertEqual(self.agent.sessions.session(7, "claude"), "sess-A")

    def test_turns_for_one_chat_do_not_overlap(self) -> None:
        slow = self.sandbox.root / "claude"
        slow.write_text(
            "#!/bin/sh\ncat > /dev/null\n"
            'echo start >> "$ALFRED_HOME/order.log"; sleep 0.3; echo end >> "$ALFRED_HOME/order.log"\n'
            'printf \'{"type":"result","is_error":false,"session_id":"s","result":"ok"}\\n\'\n',
            encoding="utf-8",
        )
        slow.chmod(0o755)
        self.sandbox.environ["ALFRED_CLAUDE_BIN"] = str(slow)

        async def both() -> None:
            first = asyncio.ensure_future(self.agent.ask(7, "one"))
            await asyncio.sleep(0.05)
            self.assertTrue(self.agent.is_busy(7))
            self.assertFalse(self.agent.is_busy(8))
            await asyncio.gather(first, self.agent.ask(7, "two"))

        asyncio.run(both())
        self.assertEqual((self.sandbox.home / "order.log").read_text().split(), ["start", "end", "start", "end"])

    def test_submit_reports_through_the_listener_and_notes_what_the_turn_changed(self) -> None:
        events = []

        class Recorder(Listener):
            async def turn_started(self, chat_id: int) -> None:
                events.append(("start", chat_id))

            async def progress(self, chat_id: int, note: str) -> None:
                events.append(("progress", chat_id, note))

            async def result(self, chat_id: int, result) -> None:
                events.append(("result", chat_id, result.text, result.new_skills, result.memory_changes))

        self.sandbox.environ["ALFRED_AUTOSAVE"] = "1"
        home.bootstrap(self.sandbox.config, legacy_dir=None)  # now with its git repository
        agent = Agent(self.sandbox.config, Recorder())
        learn = (
            "mkdir -p .claude/skills/weather memory/people && echo '- brother' > memory/people/sam.md && "
            "printf -- '---\\nname: weather\\ndescription: Forecasts.\\n---\\n' > .claude/skills/weather/SKILL.md && "
            'echo "$ALFRED_CHAT_ID $(command -v alfred)" > seen.txt'
        )
        lines = claude_lines("Learned.", "sess-A", tool=True)
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_run": learn}] + lines[1:]})

        async def scenario() -> None:
            try:
                await agent.submit(7, "learn the weather", user_id=99, username="pablo")
                self.assertTrue(agent.is_busy(7))
                self.assertFalse(agent.is_busy(8))
                await agent.wait_idle(7)
                self.assertIsNotNone(agent.live_pid(7))
                self.sandbox.fake_cli("claude", {"fresh": claude_lines("Nothing new.", "sess-A")})
                await agent.submit(7, "again")
                await agent.wait_idle(7)
            finally:
                await agent.shutdown()
            self.assertIsNone(agent.live_pid(7))

        asyncio.run(scenario())

        results = [event for event in events if event[0] == "result"]
        self.assertEqual(results[0], ("result", 7, "Learned.", ["weather"], ["saved people/sam"]))
        self.assertEqual(results[1], ("result", 7, "Nothing new.", [], []))
        self.assertEqual(events[0], ("start", 7))
        self.assertIn(("progress", 7, "Bash: List files"), events)
        self.assertEqual(agent.sessions.session(7, "claude"), "sess-A")
        # It ran in the home directory with the chat id and the `alfred` launcher available.
        self.assertEqual(
            (self.sandbox.home / "seen.txt").read_text().strip(), "7 %s" % (self.sandbox.home / "bin" / "alfred")
        )
        prompt = self.sandbox.calls("claude")[0]["stdin"]
        self.assertTrue(prompt.startswith('<context source="telegram" chat_id="7" time="'))
        self.assertIn('user="@pablo" user_id="99"', prompt)
        log = home._git(self.sandbox.home, "log", "--format=%s").stdout.splitlines()
        self.assertEqual(log[0], "chat 7: learn the weather")  # the second turn changed nothing, so no commit

    def test_session_store_survives_a_corrupt_file(self) -> None:
        path = self.sandbox.root / "sessions.json"
        path.write_text("{not json", encoding="utf-8")
        store = SessionStore(path)
        self.assertIsNone(store.session(1, "claude"))
        store.set_session(1, "claude", "abc")
        self.assertEqual(SessionStore(path).session(1, "claude"), "abc")


if __name__ == "__main__":
    unittest.main()
