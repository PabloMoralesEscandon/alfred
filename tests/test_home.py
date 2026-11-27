from __future__ import annotations

import os
import subprocess
import unittest

from consiglieri import home
from fakes import Sandbox


class HomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config  # the panel's: its home holds only state

    def test_prepare_makes_only_the_panels_state_and_the_launcher(self) -> None:
        home.prepare(self.config)

        # The panel runs no agent: no manual, personality, memory, skills or history of one in the home.
        self.assertEqual(sorted(path.name for path in self.config.home.iterdir()), ["bin", "state"])
        self.assertTrue(self.config.shared_memory_dir.is_dir())  # next to the home, for the bots
        launcher = self.config.home / "bin" / "alfred"
        self.assertTrue(os.access(launcher, os.X_OK))

        # The launcher is what every bot has on PATH: inside a bot's turn it works on that bot's directory.
        record, bot_config = self.sandbox.add_bot()
        env = {"PATH": os.environ["PATH"], "ALFRED_BOT_ID": str(record.bot_id)}
        output = subprocess.run([str(launcher), "skills"], stdout=subprocess.PIPE, text=True, check=True, env=env)
        self.assertIn('"alfred:extensions"', output.stdout)
        # Outside a bot's turn there is no agent whose skills these would be.
        output = subprocess.run([str(launcher), "skills"], stdout=subprocess.PIPE, text=True, env={"PATH": os.environ["PATH"]})
        self.assertIn('"ok": false', output.stdout)
        self.assertIn("no main agent", output.stdout)

    def test_an_old_home_from_before_the_panel_is_left_as_it_was(self) -> None:
        # What the old main agent left: its manual, personality, memory, a seed skill and its history.
        self.config.home.mkdir(parents=True)
        old = {
            "CLAUDE.md": "# Alfred: operating manual",
            "AGENTS.md": "old",
            "SOUL.md": "Your name is Alfred.",
            "MEMORY.md": "# Memory\n\n- Drinks mate.\n",
            "memory/MEMORY.md": "- [people/sam](people/sam.md): Sam\n",
            "memory/people/sam.md": "- brother\n",
            ".claude/skills/reminders/SKILL.md": "---\nname: reminders\n---\n",
            "extensions/notes/.claude-plugin/plugin.json": "{}",
        }
        for name, text in old.items():
            path = self.config.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        home.prepare(self.config)
        home.prepare(self.config)

        for name, text in old.items():
            self.assertEqual((self.config.home / name).read_text(encoding="utf-8"), text, name)
        self.assertFalse((self.config.state_dir / "retired-skills").exists())
        self.assertFalse((self.config.home / ".git").exists())

    def test_memories_are_listed_by_folder_and_the_index_is_not_one_of_them(self) -> None:
        _, self.config = self.sandbox.add_bot()
        (self.config.memory_dir / "people").mkdir()
        (self.config.memory_dir / "people" / "sam.md").write_text(
            "---\nname: sam\ndescription: Sam, the user's brother: birthday\nupdated: 2026-11-11\n---\n- Born 3 May\n",
            encoding="utf-8",
        )
        (self.config.memory_dir / "loose-note.md").write_text("no frontmatter", encoding="utf-8")

        memories = home.list_memories(self.config)

        self.assertEqual([memory.key for memory in memories], ["loose-note", "people/sam"])
        self.assertEqual(memories[1].description, "Sam, the user's brother: birthday")
        self.assertEqual(memories[0].description, "")

    def test_memory_changes_between_snapshots(self) -> None:
        _, self.config = self.sandbox.add_bot()
        folder = self.config.memory_dir / "people"
        folder.mkdir()
        (folder / "sam.md").write_text("v1", encoding="utf-8")
        (folder / "ana.md").write_text("v1", encoding="utf-8")
        before = home.memory_snapshot(self.config)
        self.assertEqual(home.memory_changes(before, home.memory_snapshot(self.config)), [])

        (folder / "sam.md").write_text("v2", encoding="utf-8")
        (folder / "ana.md").unlink()
        (self.config.memory_dir / "projects").mkdir()
        (self.config.memory_dir / "projects" / "move.md").write_text("v1", encoding="utf-8")
        self.config.memory_index.write_text("index edits alone are not reported", encoding="utf-8")

        self.assertEqual(
            home.memory_changes(before, home.memory_snapshot(self.config)),
            ["saved projects/move", "updated people/sam", "forgot people/ana"],
        )

    def test_frontmatter_parsing(self) -> None:
        fields = home.parse_frontmatter(
            '---\nname: "weather"\ndescription: >\n  Look up the weather.\n  Use for forecasts: rain, sun.\n---\nbody: no\n'
        )
        self.assertEqual(fields, {"name": "weather", "description": "Look up the weather. Use for forecasts: rain, sun."})
        self.assertEqual(home.parse_frontmatter("no frontmatter"), {})

    def test_skill_without_frontmatter_falls_back_to_folder_name(self) -> None:
        _, self.config = self.sandbox.add_bot()
        folder = self.config.skills_dir / "rough-notes"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("just text", encoding="utf-8")
        local = [skill for skill in home.list_skills(self.config) if skill.source == "local"]
        self.assertEqual([skill.name for skill in local], ["rough-notes"])

    def test_autosave_commits_a_bots_directory_and_never_the_home(self) -> None:
        self.sandbox.environ["ALFRED_AUTOSAVE"] = "1"
        _, bot_config = self.sandbox.add_bot()
        self.assertFalse(home.autosave(bot_config, "nothing changed"))
        (bot_config.memory_dir / "MEMORY.md").write_text("- new fact\n", encoding="utf-8")
        self.assertTrue(home.autosave(bot_config, "learned a fact"))
        log = subprocess.run(
            ["git", "log", "--format=%s", "--name-only"], cwd=str(bot_config.workspace), stdout=subprocess.PIPE, text=True
        ).stdout
        self.assertIn("learned a fact", log)
        self.assertIn("MEMORY.md", log)

        # The panel's own configuration never commits, even in an old home that still has a repository.
        subprocess.run(["git", "init", "-q"], cwd=str(self.config.home), check=True)
        (self.config.home / "notes.md").write_text("x", encoding="utf-8")
        self.assertFalse(home.autosave(self.config, "should not happen"))


if __name__ == "__main__":
    unittest.main()
