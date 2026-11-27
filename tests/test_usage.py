from __future__ import annotations

import asyncio
import os
import unittest

from consiglieri import usage
from fakes import Sandbox


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.home.mkdir()

    def fetch(self) -> str:
        return asyncio.run(usage.fetch(self.sandbox.config, dict(os.environ)))

    def test_bar(self) -> None:
        self.assertEqual(usage.bar(0), "░" * 10)
        self.assertEqual(usage.bar(4), "░" * 10)
        self.assertEqual(usage.bar(70), "▓" * 7 + "░" * 3)
        self.assertEqual(usage.bar(100), "▓" * 10)
        self.assertEqual(usage.bar(140), "▓" * 10)

    def test_summarize_keeps_only_the_limits(self) -> None:
        report = (
            "You are currently using your subscription\n\n"
            "Current session: 5% used · resets 7:09pm (Europe/Berlin)\n"
            "Current week (Fable): 100% used\n\n"
            "Last 24h · 112 requests\n  55%  Start fresh when you switch tasks · run /clear\n"
        )
        self.assertEqual(
            usage.summarize(report),
            "Claude usage\n\nSession: 5% used\n▓░░░░░░░░░ resets 7:09pm\n\nWeek (Fable): 100% used\n▓▓▓▓▓▓▓▓▓▓",
        )

    def test_summarize_falls_back_to_the_first_paragraph_for_unknown_formats(self) -> None:
        self.assertEqual(usage.summarize("Total cost: $1.23\nTotal duration: 5m\n\nMore detail"), "Total cost: $1.23\nTotal duration: 5m")
        self.assertEqual(usage.summarize("  "), "Claude Code returned no usage information.")

    def test_fetch_reports_failures_in_one_line(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": [{"type": "result", "is_error": True, "result": "Not logged in\nRun /login"}]})
        self.assertEqual(self.fetch(), "Could not get usage: Not logged in")

        self.sandbox.fake_cli("claude", {"fresh": [], "exit": 1})
        self.assertEqual(self.fetch(), "Could not get usage: no answer from claude")

        self.sandbox.environ["CONSIGLIERI_CLAUDE_BIN"] = str(self.sandbox.root / "missing")
        self.assertIn("Could not get usage:", self.fetch())
        self.assertIn("not installed", self.fetch())


if __name__ == "__main__":
    unittest.main()
