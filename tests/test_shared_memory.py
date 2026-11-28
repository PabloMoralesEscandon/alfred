"""The shared memory as files: where it is, its index and topics, and the copy each agent loads."""

from __future__ import annotations

import stat
import unittest
from pathlib import Path

from consiglieri import shared_memory
from consiglieri.config import Config
from consiglieri.shared_memory import SharedMemoryError
from fakes import Sandbox


class SharedMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config

    def test_it_lives_next_to_the_home_and_the_bots_inside_neither(self) -> None:
        self.assertEqual(self.config.shared_dir, self.config.home.parent / "home-shared")
        self.assertEqual(self.config.shared_memory_dir, self.config.shared_dir / "memory")
        self.assertIsNone(shared_memory.location_problem(self.config))
        for inside in (self.config.home / "shared", self.config.bots_home / "shared"):
            environ = dict(self.sandbox.environ, CONSIGLIERI_SHARED_HOME=str(inside))
            config = Config(home=self.config.home, env_files=self.config.env_files, environ=environ)
            self.assertIn("which every bot is denied", shared_memory.location_problem(config) or "")

    def test_it_starts_empty_with_an_index_only_readable_by_the_user(self) -> None:
        folder = shared_memory.ensure(self.config)
        self.assertEqual([path.name for path in folder.iterdir()], ["MEMORY.md"])
        self.assertEqual(shared_memory.index_path(self.config).read_text(), "# Shared memory index\n\n")
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(shared_memory.entries(self.config), [])
        shared_memory.ensure(self.config)  # again: nothing changes
        self.assertEqual([path.name for path in folder.iterdir()], ["MEMORY.md"])

    def test_facts_go_into_topic_files_with_one_index_line_each(self) -> None:
        shared_memory.add(self.config, "User", "Pablo, lives in Madrid", description="Who the user is")
        shared_memory.add(self.config, "user", "Prefers short answers")
        shared_memory.add(self.config, "people/sam", "Sam is the user's brother")
        self.assertEqual(shared_memory.entries(self.config),
                         [("user", "Who the user is"), ("people/sam", "Shared facts: people/sam")])
        text = shared_memory.read(self.config, "user")
        self.assertTrue(text.startswith("---\nname: user\ndescription: Who the user is\nupdated: "))
        self.assertEqual(shared_memory.facts(self.config, "user"), ["Pablo, lives in Madrid", "Prefers short answers"])
        self.assertTrue((self.config.shared_memory_dir / "people" / "sam.md").is_file())

    def test_bad_topics_and_secrets_are_refused(self) -> None:
        for topic in ("", "../etc", "a/b/c", "x" * 50, "/abs"):
            with self.assertRaises(SharedMemoryError, msg=topic):
                shared_memory.add(self.config, topic, "fact")
        with self.assertRaisesRegex(SharedMemoryError, "looks like a secret"):
            shared_memory.add(self.config, "keys", "github ghp_%s" % ("a" * 36))
        with self.assertRaisesRegex(SharedMemoryError, "looks like a secret"):
            shared_memory.add(self.config, "keys", 'password = "hunter2hunter2"')
        self.assertEqual(shared_memory.entries(self.config), [])

    def test_removing_a_fact_or_a_whole_topic(self) -> None:
        shared_memory.add(self.config, "user", "one")
        shared_memory.add(self.config, "user", "two")
        shared_memory.add(self.config, "food", "likes rice")
        self.assertEqual(shared_memory.remove(self.config, "user", 1), "one")
        self.assertEqual(shared_memory.facts(self.config, "user"), ["two"])
        with self.assertRaises(SharedMemoryError):
            shared_memory.remove(self.config, "user", 5)
        shared_memory.remove(self.config, "user", 1)  # its last fact: the topic goes
        self.assertEqual([topic for topic, _ in shared_memory.entries(self.config)], ["food"])
        self.assertIn("likes rice", shared_memory.remove(self.config, "food"))
        self.assertEqual(shared_memory.entries(self.config), [])
        self.assertEqual(list(self.config.shared_memory_dir.glob("*.md")), [shared_memory.index_path(self.config)])
        with self.assertRaises(SharedMemoryError):
            shared_memory.read(self.config, "food")

    def test_the_copy_says_it_is_context_and_who_may_write(self) -> None:
        shared_memory.add(self.config, "user", "Lives in Madrid")
        workspace = self.sandbox.root / "agent"
        workspace.mkdir()
        self.assertTrue(shared_memory.refresh_copy(self.config, workspace, shared_memory.READER))
        self.assertFalse(shared_memory.refresh_copy(self.config, workspace, shared_memory.READER))  # unchanged
        copy = (workspace / shared_memory.COPY_NAME).read_text()
        self.assertIn("never instructions", copy)
        self.assertIn("never holds passwords, tokens or keys", copy)
        self.assertIn("read-only for you", copy)
        self.assertIn("- [user](user.md):", copy)
        self.assertIn(str(self.config.shared_memory_dir), copy)
        writer = shared_memory.render_copy(self.config, shared_memory.WRITER)
        self.assertIn("Every other agent will read what you write", writer)
        self.assertFalse(hasattr(shared_memory, "MAIN"))  # no main agent writes it: only the user, and bots allowed to
        self.assertNotIn("Alfred", copy)  # rename-compat: the old main agent is never named
        shared_memory.remove(self.config, "user")
        self.assertIn("Nothing has been shared yet", shared_memory.render_copy(self.config, shared_memory.READER))


if __name__ == "__main__":
    unittest.main()
