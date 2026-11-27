from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from consiglieri import cli, home, orca
from consiglieri.agent import Agent
from fakes import Sandbox, claude_lines, install_fake_orca


class OrcaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config
        home.prepare(self.config)

    def calls(self, state):
        return json.loads(state.read_text())["calls"]

    def test_detached_env_drops_terminal_context_but_keeps_remote_selection(self) -> None:
        env = {"PATH": "/bin", "ORCA_WORKTREE_ID": "x", "ORCA_AGENT_HOOK_PORT": "1", "ORCA_PAIRING_CODE": "orca://pair"}
        self.assertEqual(orca.detached_env(env), {"PATH": "/bin", "ORCA_PAIRING_CODE": "orca://pair"})

    def test_setup_registers_the_home_and_proves_tabs_open_there(self) -> None:
        state = install_fake_orca(self.sandbox)
        self.sandbox.environ["ORCA_WORKTREE_ID"] = "the-terminal-alfred-was-started-from"

        steps = orca.setup_browser(self.config)

        self.assertEqual([label for label, _, _ in steps], ["Orca CLI", "Orca runtime", "Workspace", "Page load", "Tab scoping"])
        self.assertTrue(all(ok for _, ok, _ in steps), steps)
        self.assertIn("registered", steps[2][2])
        calls = self.calls(state)
        self.assertIn(["repo", "add", "--path", str(self.config.home)], [call["argv"] for call in calls])
        self.assertTrue(all(os.path.realpath(call["cwd"]) == os.path.realpath(str(self.config.home)) for call in calls))
        self.assertTrue(all(call["orca_env"] == [] for call in calls))
        self.assertEqual(json.loads(state.read_text())["tabs"], [])  # the test tab was closed

        again = orca.setup_browser(self.config, test=False)
        self.assertEqual([(label, ok, detail) for label, ok, detail in again][2], ("Workspace", True, "already registered"))
        self.assertEqual(orca.summary(self.config), "ready")

    def test_setup_starts_orca_when_it_is_not_running(self) -> None:
        state = install_fake_orca(self.sandbox, running=False, registered=True)
        self.assertIn("not running", orca.summary(self.config))
        steps = orca.setup_browser(self.config, test=False)
        self.assertTrue(all(ok for _, ok, _ in steps), steps)
        self.assertIn(["open"], [call["argv"] for call in self.calls(state)])

    def test_missing_cli_is_reported_not_raised(self) -> None:
        self.sandbox.environ["CONSIGLIERI_ORCA_BIN"] = str(self.sandbox.root / "no-orca")
        steps = orca.setup_browser(self.config)
        self.assertEqual(len(steps), 1)
        self.assertFalse(steps[0][1])
        self.assertIn("not found", orca.summary(self.config))
        with self.assertRaises(orca.OrcaError):
            orca.call(self.config, "status")

    def test_unlinked_home_is_called_out(self) -> None:
        install_fake_orca(self.sandbox)
        self.assertIn("alfred browser-setup", orca.summary(self.config))

    def test_screenshot_command_saves_a_file_instead_of_printing_the_image(self) -> None:
        state = install_fake_orca(self.sandbox, registered=True)
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox.environ), redirect_stdout(buffer):
            code = cli.main(["screenshot", "--page", "page-1", "--full"])
        payload = json.loads(buffer.getvalue())

        self.assertEqual((code, payload["ok"]), (0, True))
        self.assertTrue(payload["path"].startswith(str(self.config.home / "outbox" / "screenshot-")))
        with open(payload["path"], "rb") as handle:
            self.assertEqual(handle.read(), b"PNG:full-screenshot")
        self.assertEqual(self.calls(state)[-1]["argv"], ["full-screenshot", "--format", "png", "--page", "page-1"])
        self.assertLess(len(buffer.getvalue()), 300)

    def test_screenshot_command_reports_orca_errors_as_json(self) -> None:
        self.sandbox.environ["CONSIGLIERI_ORCA_BIN"] = str(self.sandbox.root / "no-orca")
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox.environ), redirect_stdout(buffer):
            code = cli.main(["screenshot"])
        self.assertEqual(code, 1)
        self.assertIn("not installed", json.loads(buffer.getvalue())["error"])

    def test_agent_turns_do_not_inherit_the_launching_terminals_orca_context(self) -> None:
        probe = self.sandbox.root / "claude"
        probe.write_text(
            "#!/bin/sh\ncat > /dev/null\n"
            'printf \'{"type":"result","is_error":false,"session_id":"s","result":"[%s][%s]"}\\n\' '
            '"$ORCA_WORKTREE_ID" "$ORCA_PAIRING_CODE"\n',
            encoding="utf-8",
        )
        probe.chmod(0o755)
        self.sandbox.environ["CONSIGLIERI_CLAUDE_BIN"] = str(probe)
        with mock.patch.dict(os.environ, {"ORCA_WORKTREE_ID": "someone-elses", "ORCA_PAIRING_CODE": "pair"}):
            result = asyncio.run(Agent(self.sandbox.add_bot()[1]).ask(1, "hi"))
        self.assertEqual(result.text, "[][pair]")

    def test_browser_setting_switches_claude_to_chrome(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("ok")})
        agent = Agent(self.sandbox.add_bot()[1])
        asyncio.run(agent.ask(1, "hi"))
        self.assertNotIn("--chrome", self.sandbox.calls("claude")[0]["argv"])
        self.assertEqual(self.config.browser, "orca")

        self.sandbox.environ["CONSIGLIERI_BROWSER"] = "Chrome"
        self.sandbox.environ["CONSIGLIERI_CLAUDE_EXTRA_ARGS"] = "--chrome"  # set both ways: still passed once
        asyncio.run(agent.ask(2, "hi"))
        self.assertEqual(self.sandbox.calls("claude")[1]["argv"].count("--chrome"), 1)
        self.assertIn("Claude in Chrome", orca.summary(self.config))

        self.sandbox.environ["CONSIGLIERI_BROWSER"] = "none"
        self.assertEqual(orca.summary(self.config), "off")
        self.sandbox.environ["CONSIGLIERI_BROWSER"] = "netscape"
        self.assertEqual(self.config.browser, "orca")

    def test_extra_claude_arguments_are_appended(self) -> None:
        self.sandbox.environ["CONSIGLIERI_CLAUDE_EXTRA_ARGS"] = '--chrome --effort "high"'
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("ok")})
        asyncio.run(Agent(self.sandbox.add_bot()[1]).ask(1, "hi"))
        self.assertEqual(self.sandbox.calls("claude")[0]["argv"][-3:], ["--chrome", "--effort", "high"])


if __name__ == "__main__":
    unittest.main()
