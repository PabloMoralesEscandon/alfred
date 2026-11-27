"""What is left of the old name on purpose, until an install is migrated (docs/RENAME_PLAN.md).

This file names the old `alfred` command and `ALFRED_*` settings on purpose: it is listed in `.rename-allow`.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from consiglieri import config as config_module
from consiglieri.agent import Agent
from consiglieri.config import Config, env_get, in_bot_turn, legacy_name, with_legacy_names

from fakes import Sandbox


class LegacySettingsTests(unittest.TestCase):
    """Phase 3: every CONSIGLIERI_X also reads ALFRED_X from the same place; the new name wins."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config
        self.env_file = self.sandbox.root / "repo.env"

    def test_legacy_name(self) -> None:
        self.assertEqual(legacy_name("CONSIGLIERI_TIMEOUT"), "ALFRED_TIMEOUT")
        self.assertEqual(legacy_name("TELEGRAM_BOT_TOKEN"), "")

    def test_only_the_old_name(self) -> None:
        self.env_file.write_text("ALFRED_TIMEOUT=30\nALFRED_ALLOWED_USERS=pablo\n", encoding="utf-8")
        self.assertEqual(self.config.timeout_seconds, 30.0)
        self.assertTrue(self.config.is_allowed(1, "pablo"))
        self.sandbox.environ["ALFRED_BACKEND"] = "codex"
        self.assertEqual(self.config.default_backend, "codex")

    def test_only_the_new_name(self) -> None:
        self.env_file.write_text("CONSIGLIERI_TIMEOUT=31\n", encoding="utf-8")
        self.assertEqual(self.config.timeout_seconds, 31.0)

    def test_both_the_new_one_wins_and_it_is_logged_once(self) -> None:
        config_module._warned.discard("CONSIGLIERI_TIMEOUT")
        self.env_file.write_text("ALFRED_TIMEOUT=30\nCONSIGLIERI_TIMEOUT=31\n", encoding="utf-8")
        with self.assertLogs("consiglieri.config", "WARNING") as logs:
            self.assertEqual(self.config.timeout_seconds, 31.0)
            self.assertEqual(self.config.timeout_seconds, 31.0)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("ALFRED_TIMEOUT", logs.output[0])

    def test_each_place_in_turn(self) -> None:
        # An old name in the environment beats a new one in a file: the places keep their order.
        self.env_file.write_text("CONSIGLIERI_TIMEOUT=31\n", encoding="utf-8")
        self.sandbox.environ["ALFRED_TIMEOUT"] = "40"
        self.assertEqual(self.config.timeout_seconds, 40.0)
        # A bot's own setting under the old name beats the environment.
        self.config.overrides = lambda: {"ALFRED_TIMEOUT": "50"}
        self.assertEqual(self.config.timeout_seconds, 50.0)

    def test_load_takes_the_old_home_and_env_file(self) -> None:
        home = self.sandbox.root / "old-home"
        env_file = self.sandbox.root / "other.env"
        loaded = Config.load({"ALFRED_HOME": str(home), "ALFRED_ENV_FILE": str(env_file)})
        self.assertEqual(loaded.home, home.resolve())
        self.assertEqual(loaded.env_files, [home.resolve() / ".env", env_file])
        newer = Config.load({"ALFRED_HOME": str(home), "CONSIGLIERI_HOME": str(self.sandbox.root / "new")})
        self.assertEqual(newer.home, (self.sandbox.root / "new").resolve())

    def test_env_get(self) -> None:
        self.assertEqual(env_get("CONSIGLIERI_CHAT_ID", environ={"ALFRED_CHAT_ID": "7"}), "7")
        self.assertEqual(env_get("CONSIGLIERI_CHAT_ID", environ={"ALFRED_CHAT_ID": "7", "CONSIGLIERI_CHAT_ID": "8"}), "8")
        self.assertEqual(env_get("CONSIGLIERI_CHAT_ID", "x", environ={}), "x")

    def test_a_bot_turn_is_seen_under_either_name(self) -> None:
        self.assertFalse(in_bot_turn({}))
        self.assertFalse(in_bot_turn({"CONSIGLIERI_BOT_ID": "0"}))
        self.assertTrue(in_bot_turn({"CONSIGLIERI_BOT_ID": "5"}))
        self.assertTrue(in_bot_turn({"ALFRED_BOT_ID": "5"}))
        self.assertTrue(in_bot_turn({"ALFRED_BOT_KEY": "abc"}))
        # An empty or zero new name never hides the old one.
        self.assertTrue(in_bot_turn({"CONSIGLIERI_BOT_ID": "0", "ALFRED_BOT_ID": "5"}))
        self.assertTrue(in_bot_turn({"CONSIGLIERI_BOT_ID": "", "ALFRED_BOT_KEY": "abc"}))

    def test_the_users_commands_refuse_a_bot_with_only_the_old_names(self) -> None:
        from consiglieri import creds_cli

        with mock.patch.dict(os.environ, {"ALFRED_BOT_ID": "5"}):
            with self.assertRaises(creds_cli.CredsError):
                creds_cli.owner_only("setup")

    def test_the_agent_gets_both_names(self) -> None:
        _, config = self.sandbox.add_bot()
        with mock.patch.dict(os.environ, {"ALFRED_HOME": "/somewhere/stale", "ALFRED_BOT_ID": "1"}):
            env = Agent(config)._environment(7, 99, "pablo")
        for name in ("HOME", "BOT_ID", "BOT_KEY", "CHAT_ID", "WORKSPACE", "USER_ID", "USERNAME"):
            self.assertEqual(env["ALFRED_" + name], env["CONSIGLIERI_" + name], name)
        self.assertEqual(env["CONSIGLIERI_HOME"], str(self.sandbox.home))
        self.assertEqual(env["CONSIGLIERI_BOT_ID"], "424242")

    def test_with_legacy_names(self) -> None:
        self.assertEqual(with_legacy_names({"CONSIGLIERI_X": "1", "ALFRED_X": "old", "PATH": "p"}),
                         {"CONSIGLIERI_X": "1", "ALFRED_X": "1", "PATH": "p"})

    def test_the_soul_writer_drops_both_prefixes(self) -> None:
        from consiglieri.soul_writer import SoulWriter

        self.sandbox.environ.update({"ALFRED_SECRET_THING": "x", "CONSIGLIERI_OTHER": "y"})
        env = SoulWriter(self.config).environment()
        self.assertFalse([key for key in env if key.startswith(("ALFRED_", "CONSIGLIERI_"))])


if __name__ == "__main__":
    unittest.main()
