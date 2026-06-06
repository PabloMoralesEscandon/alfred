from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support_telegram

support_telegram.install()

import bot


TEST_USERNAME = "allowed_user"


class PromptFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)

        self.original_base_dir = bot.BASE_DIR
        self.original_memory_root = bot.MEMORY_ROOT
        bot.BASE_DIR = root
        bot.MEMORY_ROOT = root / "memory" / "chats"
        bot.message_memory.clear()
        self.addCleanup(self.restore_bot_paths)

    def restore_bot_paths(self) -> None:
        bot.BASE_DIR = self.original_base_dir
        bot.MEMORY_ROOT = self.original_memory_root
        bot.message_memory.clear()

    def test_build_prompt_includes_prompt_files_when_present(self) -> None:
        bot.BASE_DIR.joinpath("SOUL.md").write_text("Be direct.", encoding="utf-8")
        bot.BASE_DIR.joinpath("SKILLS.md").write_text("Use reminders.", encoding="utf-8")

        prompt = bot.build_prompt(123, 456, TEST_USERNAME, "Hello")

        self.assertIn("Soul instructions:\nBe direct.", prompt)
        self.assertIn("Skill instructions:\nUse reminders.", prompt)
        self.assertLess(prompt.index("Runtime context:"), prompt.index("Soul instructions:"))
        self.assertLess(prompt.index("Soul instructions:"), prompt.index("Skill instructions:"))
        self.assertLess(prompt.index("Skill instructions:"), prompt.index("Memory rules:"))
        self.assertIn("Scheduler tool rules:", prompt)
        self.assertIn("Current request:\nHello", prompt)

    def test_build_prompt_omits_missing_prompt_files(self) -> None:
        prompt = bot.build_prompt(123, 456, TEST_USERNAME, "Hello")

        self.assertNotIn("Soul instructions:", prompt)
        self.assertNotIn("Skill instructions:", prompt)
        self.assertIn("Memory rules:", prompt)
        self.assertIn("Scheduler tool rules:", prompt)

    def test_build_prompt_trims_large_prompt_file(self) -> None:
        bot.BASE_DIR.joinpath("SOUL.md").write_text(
            "A" * (bot.MAX_PROMPT_FILE_CHARS + 50),
            encoding="utf-8",
        )

        prompt = bot.build_prompt(123, 456, TEST_USERNAME, "Hello")

        soul = prompt.split("Soul instructions:\n", 1)[1].split("\n\nMemory rules:", 1)[0]
        self.assertLessEqual(len(soul), bot.MAX_PROMPT_FILE_CHARS)
        self.assertTrue(soul.endswith("[trimmed]"))

    def test_allowed_usernames_load_from_env_whitelist_tokens(self) -> None:
        original_env_path = bot.ENV_PATH
        bot.ENV_PATH = bot.BASE_DIR / ".env"
        self.addCleanup(lambda: setattr(bot, "ENV_PATH", original_env_path))
        bot.ENV_PATH.write_text(
            "\n".join(
                [
                    "# comments are ignored",
                    f"whitelisted@{TEST_USERNAME}",
                    "whitelisted@@OtherUser, ignored=value",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(bot.load_allowed_usernames(), {TEST_USERNAME, "otheruser"})
        self.assertTrue(bot.is_allowed_user(TEST_USERNAME.upper()))
        self.assertTrue(bot.is_allowed_user("@otheruser"))
        self.assertFalse(bot.is_allowed_user("stranger"))


if __name__ == "__main__":
    unittest.main()
