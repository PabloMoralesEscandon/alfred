"""The stay-open Claude process: sleeping, waking, mid-turn messages, and the ways it can go away."""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any, Awaitable, Callable, List, Optional

from alfred.backends import BackendError, LiveEvents, LiveSession, RunResult
from alfred.backends.claude import ClaudeBackend
from fakes import CLAUDE_SESSION_GONE, Sandbox, claude_lines


class Recorder(LiveEvents):
    def __init__(self) -> None:
        self.log: List[Any] = []
        self.results: List[RunResult] = []

    async def turn_started(self) -> None:
        self.log.append("start")

    async def progress(self, note: str) -> None:
        self.log.append(note)

    async def session_started(self, session_id: str) -> None:
        self.log.append(("session", session_id))

    async def result(self, result: RunResult) -> None:
        self.log.append(("result", result.text))
        self.results.append(result)


class LiveSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.home.mkdir(parents=True)
        self.recorder = Recorder()

    def open(self, session_id: Optional[str] = None) -> LiveSession:
        return ClaudeBackend(self.sandbox.config).open_live(
            session_id=session_id, cwd=self.sandbox.home, env=dict(self.sandbox.environ), events=self.recorder
        )

    def play(self, scenario: Callable[[LiveSession], Awaitable[None]], session_id: Optional[str] = None) -> None:
        async def run() -> None:
            session = self.open(session_id)
            try:
                await asyncio.wait_for(scenario(session), timeout=20)
            finally:
                await session.close()

        asyncio.run(run())

    def test_one_process_answers_message_after_message_and_sleeps_in_between(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("First.", "sess-A", tool=True)})

        async def scenario(session: LiveSession) -> None:
            self.assertFalse(session.alive)  # nothing runs until there is something to say
            await session.send("one")
            self.assertTrue(session.busy)
            await session.wait_idle()
            pid = session.pid
            self.sandbox.fake_cli("claude", {"fresh": claude_lines("Second.", "sess-A")})
            await session.send("two")
            await session.wait_idle()
            self.assertEqual(session.pid, pid)
            self.assertTrue(session.alive)
            self.assertFalse(session.busy)

        self.play(scenario)

        self.assertEqual([result.text for result in self.recorder.results], ["First.", "Second."])
        self.assertEqual(self.recorder.results[0].session_id, "sess-A")
        self.assertIn(("session", "sess-A"), self.recorder.log)
        self.assertIn("Bash: List files", self.recorder.log)
        self.assertEqual(self.recorder.log[0], "start")
        calls = self.sandbox.calls("claude")
        self.assertEqual([call["stdin"] for call in calls], ["one", "two"])
        self.assertIn("--input-format", calls[0]["argv"])
        self.assertNotIn("--resume", calls[0]["argv"])

    def test_a_stored_session_is_resumed(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Back.", "sess-A")})

        async def scenario(session: LiveSession) -> None:
            await session.send("hi")
            await session.wait_idle()

        self.play(scenario, session_id="sess-A")

        argv = self.sandbox.calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-A")
        self.assertFalse(self.recorder.results[0].session_restarted)

    def test_a_message_sent_mid_turn_is_answered_by_the_same_result(self) -> None:
        lines = claude_lines("Both done.", "sess-A")
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_absorb": 1}] + lines[1:]})

        async def scenario(session: LiveSession) -> None:
            await session.send("do the thing")
            await session.send("and this too")
            await session.wait_idle()

        self.play(scenario)

        self.assertEqual([result.text for result in self.recorder.results], ["Both done."])
        self.assertEqual([call["stdin"] for call in self.sandbox.calls("claude")], ["do the thing", "and this too"])

    def test_it_can_wake_up_by_itself(self) -> None:
        woke = claude_lines("The download finished.", "sess-A")
        self.sandbox.fake_cli(
            "claude", {"fresh": claude_lines("Started.", "sess-A") + [{"_later": woke, "_after": 0.3}]}
        )

        async def scenario(session: LiveSession) -> None:
            await session.send("start the download")
            await session.wait_idle()
            self.assertEqual(len(self.recorder.results), 1)
            for _ in range(100):
                if len(self.recorder.results) == 2:
                    break
                await asyncio.sleep(0.05)
            await session.wait_idle()

        self.play(scenario)

        self.assertEqual([result.text for result in self.recorder.results], ["Started.", "The download finished."])
        self.assertEqual(self.recorder.log.count("start"), 3)  # the message, its turn, and the self-started turn

    def test_a_cli_that_does_not_report_per_message_progress_still_goes_idle(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-A"), "lifecycle": False})

        async def scenario(session: LiveSession) -> None:
            await session.send("hello")
            await session.wait_idle()

        self.play(scenario)
        self.assertEqual([result.text for result in self.recorder.results], ["Hi."])

    def test_a_turn_the_cli_cancels_still_ends(self) -> None:
        # What Claude Code does for a model name it does not know: an error result, then "cancelled".
        failed = claude_lines("There's an issue with the selected model.", "sess-A")
        failed[-1]["is_error"] = True
        self.sandbox.fake_cli("claude", {"fresh": failed, "final_state": "cancelled"})

        async def scenario(session: LiveSession) -> None:
            await session.send("hello?")
            await session.wait_idle()
            self.assertFalse(session.busy)

        self.play(scenario)
        self.assertTrue(self.recorder.results[0].is_error)

    def test_a_session_that_no_longer_exists_starts_fresh_and_keeps_the_message(self) -> None:
        self.sandbox.fake_cli(
            "claude", {"fresh": claude_lines("Hello again.", "sess-B"), "resume": CLAUDE_SESSION_GONE, "resume_exit": 1}
        )

        async def scenario(session: LiveSession) -> None:
            await session.send("still there?")
            await session.wait_idle()
            self.assertEqual(session.session_id, "sess-B")
            await session.send("good")
            await session.wait_idle()

        self.play(scenario, session_id="sess-gone")

        first, second = self.recorder.results
        self.assertEqual((first.text, first.session_restarted, first.is_error), ("Hello again.", True, False))
        self.assertFalse(second.session_restarted)
        calls = self.sandbox.calls("claude")
        self.assertEqual([call["stdin"] for call in calls], ["still there?", "good"])
        self.assertNotIn("--resume", calls[0]["argv"])

    def test_a_process_that_dies_mid_turn_is_reported_and_replaced_on_the_next_message(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("x", "sess-A")[:1] + [{"_exit": 3}]})

        async def scenario(session: LiveSession) -> None:
            await session.send("boom")
            await session.wait_idle()
            self.assertFalse(session.alive)
            self.sandbox.fake_cli("claude", {"fresh": claude_lines("Recovered.", "sess-A")})
            await session.send("again")
            await session.wait_idle()

        self.play(scenario)

        crashed, recovered = self.recorder.results
        self.assertTrue(crashed.is_error)
        self.assertIn("claude stopped before finishing (exit code 3)", crashed.text)
        self.assertEqual(recovered.text, "Recovered.")
        argv = self.sandbox.calls("claude")[1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-A")

    def test_a_turn_with_no_sign_of_life_is_stopped(self) -> None:
        self.sandbox.environ["ALFRED_TIMEOUT"] = "0.4"
        self.sandbox.fake_cli("claude", {"fresh": [{"_sleep": 30}]})

        async def scenario(session: LiveSession) -> None:
            await session.send("hang")
            await session.wait_idle()
            self.assertFalse(session.alive)

        self.play(scenario)

        self.assertTrue(self.recorder.results[0].is_error)
        self.assertIn("no sign of life", self.recorder.results[0].text)
        with self.assertRaises(ProcessLookupError):
            os.kill(self.sandbox.live_pid("claude"), 0)

    def test_close_kills_the_process_and_refuses_more_messages(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": [{"_sleep": 30}]})

        async def scenario(session: LiveSession) -> None:
            await session.send("long job")
            for _ in range(100):
                if self.sandbox.calls("claude"):
                    break
                await asyncio.sleep(0.05)
            await session.close()
            self.assertFalse(session.busy)
            with self.assertRaises(BackendError):
                await session.send("more")

        self.play(scenario)

        self.assertEqual(self.recorder.results, [])
        with self.assertRaises(ProcessLookupError):
            os.kill(self.sandbox.live_pid("claude"), 0)

    def test_missing_cli_is_a_backend_error(self) -> None:
        self.sandbox.environ["ALFRED_CLAUDE_BIN"] = str(self.sandbox.root / "missing")

        async def scenario(session: LiveSession) -> None:
            with self.assertRaises(BackendError):
                await session.send("hi")

        self.play(scenario)


if __name__ == "__main__":
    unittest.main()
