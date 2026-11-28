from __future__ import annotations

import unittest

from fakes import Sandbox


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config
        self.env_file = self.sandbox.root / "repo.env"

    def test_legacy_whitelist_tokens_still_work(self) -> None:
        self.env_file.write_text(
            "# comment whitelisted@ignored_in_comment\nwhitelisted@Allowed_User\nwhitelisted@@Other, junk=1\n"
            "TELEGRAM_BOT_TOKEN=abc:def\n",
            encoding="utf-8",
        )
        self.assertEqual(self.config.allowed_users(), {"allowed_user", "other"})
        self.assertTrue(self.config.is_allowed(1, "ALLOWED_USER"))
        self.assertTrue(self.config.is_allowed(1, "@other"))
        self.assertFalse(self.config.is_allowed(1, "stranger"))
        self.assertFalse(self.config.is_allowed(1, None))
        self.assertEqual(self.config.telegram_token, "abc:def")

    def test_allowed_users_accepts_ids_and_names_and_reloads(self) -> None:
        self.env_file.write_text('CONSIGLIERI_ALLOWED_USERS="12345, @Pablo"\n', encoding="utf-8")
        self.assertTrue(self.config.is_allowed(12345, None))
        self.assertTrue(self.config.is_allowed(999, "pablo"))
        self.assertFalse(self.config.is_allowed(999, "someone"))
        self.env_file.write_text("CONSIGLIERI_ALLOWED_USERS=someone\n", encoding="utf-8")
        self.assertTrue(self.config.is_allowed(999, "someone"))
        self.assertFalse(self.config.is_allowed(12345, None))

    def test_nobody_is_allowed_by_default(self) -> None:
        self.assertFalse(self.config.is_allowed(1, "anyone"))

    def test_environment_beats_env_file_and_defaults_apply(self) -> None:
        self.env_file.write_text("CONSIGLIERI_BACKEND=codex\nCONSIGLIERI_TIMEOUT=30 # seconds\n", encoding="utf-8")
        self.assertEqual(self.config.default_backend, "codex")
        self.assertEqual(self.config.timeout_seconds, 30.0)
        self.sandbox.environ["CONSIGLIERI_BACKEND"] = "claude"
        self.assertEqual(self.config.default_backend, "claude")
        self.assertEqual(self.config.claude_permission_mode, "bypassPermissions")

    def test_missing_token_is_a_clear_error(self) -> None:
        self.env_file.write_text("TELEGRAM_BOT_TOKEN=replace_me\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.config.telegram_token


if __name__ == "__main__":
    unittest.main()
