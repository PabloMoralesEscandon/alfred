from __future__ import annotations

import io
import os
import plistlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from alfred import cli, service
from alfred.config import REPO_ROOT
from fakes import Sandbox, claude_lines


class ServiceTests(unittest.TestCase):
    """Unit files and safety checks. Nothing here touches the real launchctl or systemctl."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config

    def make_ready(self) -> None:
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "99"
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("ok")})

    def test_environment_records_a_deduplicated_path_and_the_home(self) -> None:
        self.sandbox.environ["PATH"] = "/a/bin:/b/bin:/a/bin::/c/bin"
        env = service.service_environment(self.config)
        self.assertEqual(env["PATH"], "/a/bin:/b/bin:/c/bin")
        self.assertEqual(env["ALFRED_HOME"], str(self.config.home))
        self.assertEqual(env["PYTHONPATH"], str(REPO_ROOT))

    def test_launchd_plist_restarts_forever_and_logs_to_the_home(self) -> None:
        plist = plistlib.loads(service.launchd_plist(self.config))
        self.assertEqual(plist["Label"], "com.alfred.bot")
        self.assertEqual(plist["ProgramArguments"], [sys.executable, "-m", "alfred", "run"])
        self.assertIs(plist["KeepAlive"], True)
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(plist["StandardErrorPath"], str(self.config.state_dir / "alfred.log"))
        self.assertEqual(plist["EnvironmentVariables"]["ALFRED_HOME"], str(self.config.home))
        self.assertNotIn("TELEGRAM_BOT_TOKEN", plist["EnvironmentVariables"])  # secrets stay in .env

    def test_systemd_unit(self) -> None:
        unit = service.systemd_unit(self.config)
        self.assertIn('ExecStart="%s" "-m" "alfred" "run"' % sys.executable, unit)
        self.assertIn("Restart=always", unit)
        self.assertIn('Environment="ALFRED_HOME=%s"' % self.config.home, unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", unit)

    def test_preflight_names_everything_that_would_make_the_bot_crash_loop(self) -> None:
        self.sandbox.environ["ALFRED_CLAUDE_BIN"] = str(self.sandbox.root / "missing-claude")
        problems = service.preflight(self.config)
        self.assertEqual(len(problems), 3)
        self.assertIn("TELEGRAM_BOT_TOKEN", problems[0])
        self.assertIn("ALFRED_ALLOWED_USERS", problems[1])
        self.assertIn("missing-claude", problems[2])

        self.make_ready()
        self.assertEqual(service.preflight(self.config), [])

        self.sandbox.environ["ALFRED_BACKEND"] = "codex"
        self.sandbox.environ["ALFRED_CODEX_BIN"] = str(self.sandbox.root / "missing-codex")
        self.assertIn("missing-codex", service.preflight(self.config)[0])

    def test_install_refuses_before_touching_the_service_manager(self) -> None:
        with mock.patch.object(service, "launchd_install") as launchd, mock.patch.object(service, "_systemctl") as systemctl:
            with self.assertRaises(service.ServiceError) as caught:
                service.install(self.config)
        self.assertIn("would not start", str(caught.exception))
        launchd.assert_not_called()
        systemctl.assert_not_called()

    def test_install_goes_ahead_when_ready(self) -> None:
        self.make_ready()
        with mock.patch.object(service, "platform", return_value="launchd"), \
                mock.patch.object(service, "launchd_install", return_value=service.launchd_plist_path()) as launchd:
            path, notes = service.install(self.config)
        launchd.assert_called_once_with(self.config)
        self.assertEqual(path, service.launchd_plist_path())
        self.assertIn("automatic login", notes[0])

    def test_status_wording(self) -> None:
        cases = [
            ({"installed": "no", "state": "not loaded", "pid": None}, "not installed"),
            ({"installed": "yes", "state": "running", "pid": "42", "last_exit": None}, "running (pid 42)"),
            ({"installed": "yes", "state": "spawn scheduled", "pid": None, "last_exit": "1"},
             "installed, spawn scheduled, last exit code 1"),
            ({"installed": "yes", "state": "not loaded", "pid": None}, "installed, not loaded"),
        ]
        for info, expected in cases:
            with mock.patch.object(service, "platform", return_value="launchd"), \
                    mock.patch.object(service, "launchd_status", return_value=info):
                self.assertEqual(service.status(), expected)

    def test_logs_command_and_cli_refusal(self) -> None:
        environ = dict(self.sandbox.environ)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["service", "logs"]), 0)
            self.assertIn("no log yet", out.getvalue())
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            service.log_path(self.config).write_text("\n".join("line %d" % n for n in range(100)), encoding="utf-8")
            out.truncate(0)
            self.assertEqual(cli.main(["service", "logs", "-n", "2"]), 0)
            self.assertTrue(out.getvalue().strip().endswith("line 98\nline 99"))
            self.assertNotIn("line 97", out.getvalue())
            with mock.patch.object(service, "launchd_install") as launchd:
                self.assertEqual(cli.main(["service", "install"]), 1)
            launchd.assert_not_called()
        self.assertIn("would not start", err.getvalue())


if __name__ == "__main__":
    unittest.main()
