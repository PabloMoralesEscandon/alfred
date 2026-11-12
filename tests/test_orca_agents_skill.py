"""The base `orca-agents` skill and the `alfred-wait-agent` watcher it tells the agent to run."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from typing import Any, Dict, List, Optional

from alfred import extensions, home
from fakes import Sandbox

# Replays one canned `orca worktree ps --json` reply per call; the last one repeats.
FAKE_ORCA_PS = """#!{python}
import json, sys
path = {replies!r}
replies = json.load(open(path))
reply = replies.pop(0) if len(replies) > 1 else replies[0]
json.dump(replies, open(path, "w"))
if reply == "broken":
    print("not json")
    sys.exit(1)
print(json.dumps(reply))
"""


def listing(state: Optional[str], message: str = "") -> Dict[str, Any]:
    agents = [] if state is None else [
        {"paneKey": "tab:leaf", "state": state, "agentType": "codex", "prompt": "Fix the tests",
         "lastAssistantMessage": message, "interrupted": False},
        {"paneKey": "other:pane", "state": "working", "agentType": "claude"},
    ]
    return {"ok": True, "result": {"worktrees": [
        {"path": "/repos/quiet", "repo": "quiet", "agents": []},
        {"path": "/repos/mus-solver", "repo": "mus-solver", "agents": agents},
    ]}}


class OrcaAgentsSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        home.prepare(self.sandbox.config)
        self.script = extensions.BASE_PLUGIN / "bin" / "alfred-wait-agent"

    def wait(self, replies: List[Any], *extra: str) -> Dict[str, Any]:
        path = self.sandbox.root / "replies.json"
        path.write_text(json.dumps(replies), encoding="utf-8")
        orca = self.sandbox.root / "orca"
        orca.write_text(FAKE_ORCA_PS.format(python=sys.executable, replies=str(path)), encoding="utf-8")
        orca.chmod(orca.stat().st_mode | stat.S_IXUSR)
        completed = subprocess.run(
            [sys.executable, str(self.script), "--pane", "tab:leaf", "--interval", "0.02", *extra],
            env=dict(os.environ, ALFRED_ORCA_BIN=str(orca)), stdout=subprocess.PIPE, text=True, timeout=30,
        )
        return dict(json.loads(completed.stdout), returncode=completed.returncode)

    def test_the_skill_ships_in_the_base_with_its_command(self) -> None:
        skills = {skill.name: skill for skill in home.list_skills(self.sandbox.config)}
        self.assertEqual(skills["alfred:orca-agents"].source, "base")
        self.assertIn("Use for", skills["alfred:orca-agents"].description)
        self.assertTrue(os.access(self.script, os.X_OK))
        self.assertIn("alfred-wait-agent --pane", skills["alfred:orca-agents"].path.read_text())

    def test_waits_through_the_launch_and_a_pause_between_steps(self) -> None:
        replies = [listing(None), listing("working"), listing("done"), listing("working"),
                   listing("done", "Tests pass."), listing("done", "Tests pass.")]
        outcome = self.wait(replies)
        self.assertEqual((outcome["returncode"], outcome["ok"], outcome["state"]), (0, True, "done"))
        self.assertEqual(outcome["last_message"], "Tests pass.")
        self.assertEqual((outcome["agent"], outcome["repo"], outcome["worktree"]), ("codex", "mus-solver", "/repos/mus-solver"))

    def test_reports_a_closed_terminal_a_timeout_and_an_agent_waiting_for_someone(self) -> None:
        self.assertEqual(self.wait([listing("working"), listing(None)])["state"], "gone")
        self.assertEqual(self.wait([listing("working")], "--timeout", "0.2")["state"], "timeout")
        self.assertEqual(self.wait([listing("working"), listing("blocked")])["state"], "blocked")

    def test_gives_up_with_an_error_when_orca_cannot_be_asked(self) -> None:
        outcome = self.wait([listing("working"), "broken"])
        self.assertEqual((outcome["returncode"], outcome["ok"]), (1, False))
        self.assertIn("cannot ask Orca", outcome["error"])

        # One bad answer in between is not the end.
        self.assertEqual(self.wait([listing("working"), "broken", listing("done")])["state"], "done")


if __name__ == "__main__":
    unittest.main()
