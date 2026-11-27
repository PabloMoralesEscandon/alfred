from __future__ import annotations

import asyncio
import os
import unittest

from consiglieri.backends import BackendError, create_backend
from consiglieri.backends.claude import describe_tool_use
from fakes import CLAUDE_SESSION_GONE, Sandbox, claude_lines


class BackendTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.home.mkdir()
        self.notes = []

    def run_backend(self, name: str, prompt: str = "hello", session_id=None, env=None):
        async def on_progress(note: str) -> None:
            self.notes.append(note)

        backend = create_backend(name, self.sandbox.config)
        return asyncio.run(
            backend.run(
                prompt,
                session_id=session_id,
                cwd=self.sandbox.home,
                env=dict(os.environ) if env is None else env,
                on_progress=on_progress,
            )
        )


class ClaudeBackendTests(BackendTestCase):
    def test_fresh_run_parses_result_and_reports_tool_progress(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi there.", "sess-9", tool=True)})

        result = self.run_backend("claude", prompt="héllo")

        self.assertEqual((result.text, result.session_id, result.is_error), ("Hi there.", "sess-9", False))
        self.assertFalse(result.session_restarted)
        self.assertEqual(result.cost_usd, 0.01)
        self.assertEqual(self.notes, ["Bash: List files"])
        call = self.sandbox.calls("claude")[0]
        self.assertEqual(call["stdin"], "héllo")
        self.assertNotIn("--resume", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(call["argv"][call["argv"].index("--output-format") + 1], "stream-json")

    def test_resume_and_model_flags(self) -> None:
        self.sandbox.environ["CONSIGLIERI_CLAUDE_MODEL"] = "sonnet"
        self.sandbox.environ["CONSIGLIERI_CLAUDE_PERMISSION_MODE"] = "acceptEdits"
        self.sandbox.environ["CONSIGLIERI_CLAUDE_EFFORT"] = "High"
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("ok", "sess-9")})

        self.run_backend("claude", session_id="sess-9")

        argv = self.sandbox.calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-9")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_lost_session_is_retried_fresh(self) -> None:
        self.sandbox.fake_cli(
            "claude", {"fresh": claude_lines("Back again.", "sess-new"), "resume": CLAUDE_SESSION_GONE, "resume_exit": 1}
        )

        result = self.run_backend("claude", session_id="old")

        self.assertEqual((result.text, result.session_id, result.is_error), ("Back again.", "sess-new", False))
        self.assertTrue(result.session_restarted)
        calls = self.sandbox.calls("claude")
        self.assertEqual(len(calls), 2)
        self.assertIn("--resume", calls[0]["argv"])
        self.assertNotIn("--resume", calls[1]["argv"])

    def test_error_result_inside_a_live_session_is_not_retried(self) -> None:
        lines = [
            {"type": "system", "subtype": "init", "session_id": "sess-9"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Partial answer."}]}},
            {"type": "result", "subtype": "error_max_turns", "is_error": True, "session_id": "sess-9"},
        ]
        self.sandbox.fake_cli("claude", {"fresh": lines})

        result = self.run_backend("claude", session_id="sess-9")

        self.assertEqual((result.text, result.session_id, result.is_error), ("Partial answer.", "sess-9", True))
        self.assertEqual(len(self.sandbox.calls("claude")), 1)

    def test_crash_without_result_is_an_error(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": [], "exit": 3})
        result = self.run_backend("claude")
        self.assertTrue(result.is_error)
        self.assertIn("code 3", result.text)

    def test_parent_claude_session_variables_are_not_passed_down(self) -> None:
        probe = self.sandbox.root / "claude"
        probe.write_text(
            "#!/bin/sh\ncat > /dev/null\n"
            'printf \'{"type":"result","is_error":false,"session_id":"s","result":"[%s][%s]"}\\n\' '
            '"$CLAUDECODE" "$CONSIGLIERI_CHAT_ID"\n',
            encoding="utf-8",
        )
        probe.chmod(0o755)
        self.sandbox.environ["CONSIGLIERI_CLAUDE_BIN"] = str(probe)
        env = dict(os.environ, CLAUDECODE="1", CONSIGLIERI_CHAT_ID="42")
        self.assertEqual(self.run_backend("claude", env=env).text, "[][42]")

    def test_missing_binary_and_timeout_raise_backend_errors(self) -> None:
        self.sandbox.environ["CONSIGLIERI_CLAUDE_BIN"] = str(self.sandbox.root / "does-not-exist")
        with self.assertRaises(BackendError):
            self.run_backend("claude")

        sleeper = self.sandbox.root / "sleeper"
        sleeper.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        sleeper.chmod(0o755)
        self.sandbox.environ["CONSIGLIERI_CLAUDE_BIN"] = str(sleeper)
        self.sandbox.environ["CONSIGLIERI_TIMEOUT"] = "0.3"
        with self.assertRaises(BackendError) as caught:
            self.run_backend("claude")
        self.assertIn("timed out", str(caught.exception))

    def test_tool_descriptions(self) -> None:
        self.assertEqual(describe_tool_use({"name": "Skill", "input": {"skill": "reminders"}}), "Using skill: reminders")
        self.assertEqual(describe_tool_use({"name": "Read", "input": {"file_path": "/a/b/notes.md"}}), "Read: notes.md")
        self.assertEqual(describe_tool_use({"name": "TodoWrite", "input": {}}), "TodoWrite")
        self.assertEqual(
            describe_tool_use({"name": "mcp__claude-in-chrome__navigate", "input": {"url": "https://example.com"}}),
            "claude-in-chrome navigate: https://example.com",
        )
        self.assertEqual(
            describe_tool_use({"name": "mcp__claude-in-chrome__computer", "input": {"action": "screenshot"}}),
            "claude-in-chrome computer screenshot",
        )
        long_command = describe_tool_use({"name": "Bash", "input": {"command": "x" * 500}})
        self.assertLessEqual(len(long_command), 90)

    def test_unknown_backend(self) -> None:
        with self.assertRaises(BackendError):
            create_backend("gemini", self.sandbox.config)


class CodexBackendTests(BackendTestCase):
    FRESH = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Looking."}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done, sir."}},
        {"type": "turn.completed"},
    ]

    def test_fresh_run_uses_last_agent_message_and_thread_id(self) -> None:
        self.sandbox.fake_cli("codex", {"fresh": self.FRESH})

        result = self.run_backend("codex")

        self.assertEqual((result.text, result.session_id, result.is_error), ("Done, sir.", "thread-1", False))
        self.assertEqual(self.notes, ["Shell: ls"])
        argv = self.sandbox.calls("codex")[0]["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[-1], "-")

    def test_resume_puts_the_session_before_the_stdin_marker(self) -> None:
        self.sandbox.environ["CONSIGLIERI_CODEX_SANDBOX"] = "workspace-write"
        self.sandbox.environ["CONSIGLIERI_CODEX_EFFORT"] = "low"
        self.sandbox.fake_cli("codex", {"fresh": self.FRESH})

        self.run_backend("codex", session_id="thread-1")

        argv = self.sandbox.calls("codex")[0]["argv"]
        self.assertEqual(argv[:2], ["exec", "resume"])
        self.assertEqual(argv[-2:], ["thread-1", "-"])
        self.assertIn('sandbox_mode="workspace-write"', argv)
        self.assertNotIn("--sandbox", argv)
        self.assertIn('model_reasoning_effort="low"', argv)

    def test_turn_failure_is_reported(self) -> None:
        lines = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "model not supported"}},
        ]
        self.sandbox.fake_cli("codex", {"fresh": lines})
        result = self.run_backend("codex")
        self.assertEqual((result.text, result.is_error), ("model not supported", True))


if __name__ == "__main__":
    unittest.main()
