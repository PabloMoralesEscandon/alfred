from __future__ import annotations

import os
import subprocess
import unittest

from alfred import home
from fakes import Sandbox


class HomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config

    def test_bootstrap_creates_manual_seed_skills_and_launcher(self) -> None:
        home.bootstrap(self.config, legacy_dir=None)

        manual = (self.config.home / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@SOUL.md", manual)
        self.assertIn("@memory/MEMORY.md", manual)
        self.assertEqual(self.config.memory_index.read_text(encoding="utf-8"), "# Memory index\n\n")
        self.assertFalse((self.config.home / "MEMORY.md").exists())
        self.assertEqual(home.list_memories(self.config), [])
        self.assertTrue((self.config.home / "AGENTS.md").exists())
        self.assertEqual({skill.name for skill in home.list_skills(self.config)}, {"alfred:browser", "alfred:extensions", "alfred:orca-agents", "alfred:reminders", "alfred:web-logins"})
        self.assertEqual({skill.source for skill in home.list_skills(self.config)}, {"base"})
        self.assertEqual(list(self.config.skills_dir.iterdir()), [])  # nothing is copied into the home any more
        self.assertTrue(self.config.extensions_dir.is_dir())
        for skill in home.list_skills(self.config):
            self.assertTrue(skill.description, skill.name)

        launcher = self.config.home / "bin" / "alfred"
        self.assertTrue(os.access(launcher, os.X_OK))
        output = subprocess.run([str(launcher), "skills"], stdout=subprocess.PIPE, text=True, check=True, env={"PATH": os.environ["PATH"]})
        self.assertIn('"alfred:extensions"', output.stdout)

    def test_bootstrap_refreshes_managed_files_but_keeps_owned_ones(self) -> None:
        home.bootstrap(self.config, legacy_dir=None)
        (self.config.home / "CLAUDE.md").write_text("tampered", encoding="utf-8")
        (self.config.home / "SOUL.md").write_text("You are Jeeves.", encoding="utf-8")
        self.config.memory_index.write_text("# Memory index\n\n- [people/sam](people/sam.md): Sam\n", encoding="utf-8")
        (self.config.memory_dir / "people").mkdir()
        (self.config.memory_dir / "people" / "sam.md").write_text("- brother\n", encoding="utf-8")
        mine = self.config.skills_dir / "my-notes" / "SKILL.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("---\nname: my-notes\ndescription: written by hand\n---\n", encoding="utf-8")
        # A copy of a built-in skill left by an earlier version, which would shadow the base plugin.
        old_copy = self.config.skills_dir / "reminders" / "SKILL.md"
        old_copy.parent.mkdir(parents=True)
        old_copy.write_text("---\nname: reminders\ndescription: old seed\n---\n", encoding="utf-8")

        home.bootstrap(self.config, legacy_dir=None)

        self.assertIn("operating manual", (self.config.home / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertEqual((self.config.home / "SOUL.md").read_text(encoding="utf-8"), "You are Jeeves.")
        self.assertIn("people/sam", self.config.memory_index.read_text(encoding="utf-8"))
        self.assertEqual((self.config.memory_dir / "people" / "sam.md").read_text(encoding="utf-8"), "- brother\n")
        self.assertIn("written by hand", mine.read_text(encoding="utf-8"))
        self.assertFalse(old_copy.exists())
        self.assertTrue((self.config.state_dir / "retired-skills" / "reminders" / "SKILL.md").is_file())

    def test_legacy_layout_is_imported_once(self) -> None:
        legacy = self.sandbox.root / "old"
        (legacy / "memory" / "chats" / "1").mkdir(parents=True)
        (legacy / "memory" / "chats" / "2").mkdir(parents=True)
        (legacy / "SOUL.md").write_text("Be direct.", encoding="utf-8")
        (legacy / "SKILLS.md").write_text("# ToDoList CLI\nUse `tdl`.", encoding="utf-8")
        (legacy / "memory" / "chats" / "1" / "memory.md").write_text("- owns a cat\n", encoding="utf-8")
        (legacy / "memory" / "chats" / "2" / "memory.md").write_text("- owns a cat\n- born in May\n", encoding="utf-8")

        self.assertEqual(home.bootstrap(self.config, legacy_dir=legacy), ["SOUL.md", "SKILLS.md", "memory"])

        self.assertEqual((self.config.home / "SOUL.md").read_text(encoding="utf-8"), "Be direct.")
        carried = {memory.key: memory for memory in home.list_memories(self.config)}["general"]
        self.assertTrue(carried.path.read_text(encoding="utf-8").endswith("\n\n- owns a cat\n- born in May\n"))
        self.assertIn("first version of Alfred", carried.description)
        self.assertIn("- [general](general.md): Facts carried over", self.config.memory_index.read_text(encoding="utf-8"))
        legacy_skill = {skill.name: skill for skill in home.list_skills(self.config)}[home.LEGACY_SKILL_NAME]
        self.assertIn("Use `tdl`.", legacy_skill.path.read_text(encoding="utf-8"))
        self.assertEqual(home.bootstrap(self.config, legacy_dir=legacy), [])

    def test_single_file_memory_from_an_earlier_home_is_moved_into_the_folder(self) -> None:
        self.config.home.mkdir(parents=True)
        (self.config.home / "MEMORY.md").write_text("# Memory\n\n- Drinks mate, not coffee.\n", encoding="utf-8")

        home.bootstrap(self.config, legacy_dir=None)

        self.assertFalse((self.config.home / "MEMORY.md").exists())
        self.assertEqual([memory.key for memory in home.list_memories(self.config)], ["general"])
        self.assertIn("- Drinks mate, not coffee.", (self.config.memory_dir / "general.md").read_text(encoding="utf-8"))
        self.assertIn("(general.md)", self.config.memory_index.read_text(encoding="utf-8"))
        home.bootstrap(self.config, legacy_dir=None)  # and it does not happen twice
        self.assertEqual(self.config.memory_index.read_text(encoding="utf-8").count("(general.md)"), 1)

    def test_empty_single_file_memory_is_just_removed(self) -> None:
        self.config.home.mkdir(parents=True)
        (self.config.home / "MEMORY.md").write_text("# Memory\n\n", encoding="utf-8")
        home.bootstrap(self.config, legacy_dir=None)
        self.assertFalse((self.config.home / "MEMORY.md").exists())
        self.assertEqual(home.list_memories(self.config), [])

    def test_memories_are_listed_by_folder_and_the_index_is_not_one_of_them(self) -> None:
        home.bootstrap(self.config, legacy_dir=None)
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
        home.bootstrap(self.config, legacy_dir=None)
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
        folder = self.config.skills_dir / "rough-notes"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("just text", encoding="utf-8")
        local = [skill for skill in home.list_skills(self.config) if skill.source == "local"]
        self.assertEqual([skill.name for skill in local], ["rough-notes"])

    def test_autosave_commits_changes_to_the_home_repo(self) -> None:
        self.sandbox.environ["ALFRED_AUTOSAVE"] = "1"
        home.bootstrap(self.config, legacy_dir=None)
        self.assertFalse(home.autosave(self.config, "nothing changed"))
        (self.config.memory_dir / "MEMORY.md").write_text("- new fact\n", encoding="utf-8")
        (self.config.state_dir / "sessions.json").write_text("{}", encoding="utf-8")
        self.assertTrue(home.autosave(self.config, "learned a fact"))
        log = subprocess.run(
            ["git", "log", "--format=%s", "--name-only"], cwd=str(self.config.home), stdout=subprocess.PIPE, text=True
        ).stdout
        self.assertIn("learned a fact", log)
        self.assertIn("MEMORY.md", log)
        self.assertNotIn("sessions.json", log)  # runtime state stays out of history


if __name__ == "__main__":
    unittest.main()
