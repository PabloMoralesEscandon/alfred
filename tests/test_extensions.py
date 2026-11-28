"""Extensions: what Consiglieri adds to itself, kept apart from the base it ships with."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from consiglieri import extensions, home
from consiglieri.agent import Agent, Listener
from consiglieri.backends.claude import ClaudeBackend
from fakes import Sandbox, claude_lines

SKILL = "---\nname: forecast\ndescription: Weather forecast for a city. Use when the user asks about the weather.\n---\nRun `weather-now <city>`.\n"


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["CONSIGLIERI_CLAUDE_BIN"] = str(self.sandbox.root / "no-claude")  # no plugin validator
        # Every agent is a bot: extensions live in a bot's own directory.
        self.record, self.config = self.sandbox.add_bot()

    def make_weather(self, *, with_command: bool = True) -> Path:
        extensions.scaffold(self.config, "weather", "Weather lookups")
        root = self.config.extensions_dir / "weather"
        (root / "skills" / "forecast").mkdir()
        (root / "skills" / "forecast" / "SKILL.md").write_text(SKILL, encoding="utf-8")
        if with_command:
            command = root / "bin" / "weather-now"
            command.write_text('#!/bin/sh\necho "sunny in $1"\n', encoding="utf-8")
            command.chmod(0o755)
        return root

    def test_a_new_extension_is_a_plugin_consiglieri_loads_next_to_the_base(self) -> None:
        root = self.make_weather()

        manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((manifest["name"], manifest["version"]), ("weather", "0.1.0"))
        self.assertIn("## Changes", (root / "README.md").read_text())
        report = extensions.check(self.config, "weather")
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["needs_approval"], [])

        skills = {skill.name: skill.source for skill in home.list_skills(self.config)}
        self.assertEqual(skills["weather:forecast"], "extension")
        self.assertEqual(skills["consiglieri:extensions"], "base")
        argv = ClaudeBackend(self.config).command(None, live=True)
        plugins = [argv[index + 1] for index, flag in enumerate(argv) if flag == "--plugin-dir"]
        self.assertEqual(plugins, [str(extensions.BASE_PLUGIN), str(root)])
        self.assertEqual(extensions.bin_dirs(self.config), [extensions.BASE_PLUGIN / "bin", root / "bin"])

    def test_names_are_checked_and_never_taken_twice(self) -> None:
        for bad in ("Weather", "my_ext", "consiglieri", "claude-tools", "-x", "a" * 41):
            with self.assertRaises(extensions.ExtensionError, msg=bad):
                extensions.scaffold(self.config, bad, "something")
        extensions.scaffold(self.config, "weather", "Weather lookups")
        with self.assertRaises(extensions.ExtensionError):
            extensions.scaffold(self.config, "weather", "again")
        with self.assertRaises(extensions.ExtensionError):
            extensions.scaffold(self.config, "travel", "  ")

    def test_check_refuses_broken_or_unsafe_parts(self) -> None:
        root = self.make_weather()
        (root / "bin" / "not-executable").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "bin" / "consiglieri-wait-agent").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "bin" / "consiglieri-wait-agent").chmod(0o755)
        (root / "lib").mkdir()
        (root / "lib" / "client.py").write_text('API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"\n', encoding="utf-8")
        (root / "skills" / "vague").mkdir()
        (root / "skills" / "vague" / "SKILL.md").write_text("---\nname: vague\ndescription: stuff\n---\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text("instructions", encoding="utf-8")

        report = extensions.check(self.config, "weather")

        self.assertFalse(report["ok"])
        errors = "\n".join(report["errors"])
        for expected in (
            "bin/not-executable is not executable",
            "bin/consiglieri-wait-agent would replace one of Consiglieri's own commands",
            "lib/client.py looks like it contains a Anthropic or OpenAI key",
            "skills/vague/SKILL.md needs a description",
            "CLAUDE.md inside an extension is not loaded",
        ):
            self.assertIn(expected, errors)

    def test_parts_that_run_by_themselves_need_approval(self) -> None:
        root = self.make_weather()
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]}}), encoding="utf-8"
        )
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"radar": {"command": "radar-server"}}}), encoding="utf-8")
        (root / "monitors").mkdir()
        (root / "monitors" / "monitors.json").write_text(
            json.dumps([{"name": "storm-watch", "command": "true", "description": "Storms"}]), encoding="utf-8"
        )

        report = extensions.check(self.config, "weather")

        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["needs_approval"], ["hook on PostToolUse", "MCP server radar", "monitor storm-watch"])

    def test_tests_inside_the_extension_are_run(self) -> None:
        root = self.make_weather()
        (root / "tests").mkdir()
        test_file = root / "tests" / "test_weather.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        self.assertTrue(extensions.check(self.config, "weather")["tests"]["ok"])

        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('nope')\n", encoding="utf-8")
        report = extensions.check(self.config, "weather")
        self.assertFalse(report["ok"])
        self.assertIn("nope", report["tests"]["output"])
        self.assertTrue(extensions.check(self.config, "weather", run_tests=False)["ok"])

    def test_the_plugin_validator_is_consulted_when_claude_is_installed(self) -> None:
        self.make_weather()
        validator = self.sandbox.root / "claude"
        validator.write_text('#!/bin/sh\necho "✘ Validation failed: hooks: Invalid input"\nexit 1\n', encoding="utf-8")
        validator.chmod(0o755)
        self.sandbox.environ["CONSIGLIERI_CLAUDE_BIN"] = str(validator)

        report = extensions.check(self.config, "weather")

        self.assertFalse(report["ok"])
        self.assertIn("claude plugin validate failed: ✘ Validation failed: hooks: Invalid input", report["errors"])

    def test_disable_enable_and_remove(self) -> None:
        self.make_weather()

        extensions.set_enabled(self.config, "weather", False)
        self.assertTrue((self.config.disabled_extensions_dir / "weather").is_dir())
        self.assertEqual(extensions.plugin_dirs(self.config), [extensions.BASE_PLUGIN])
        self.assertNotIn("weather:forecast", {skill.name for skill in home.list_skills(self.config)})
        self.assertEqual([(item.name, item.enabled) for item in extensions.list_extensions(self.config)], [("weather", False)])

        extensions.set_enabled(self.config, "weather", True)
        self.assertIn("weather:forecast", {skill.name for skill in home.list_skills(self.config)})

        moved = extensions.remove(self.config, "weather")
        self.assertTrue((moved / "skills" / "forecast" / "SKILL.md").is_file())
        self.assertEqual(extensions.list_extensions(self.config), [])
        with self.assertRaises(extensions.ExtensionError):
            extensions.remove(self.config, "weather")

    def test_fingerprint_follows_what_loads_not_the_text_of_a_skill(self) -> None:
        empty = extensions.fingerprint(self.config)
        root = self.make_weather()
        added = extensions.fingerprint(self.config)
        self.assertNotEqual(empty, added)

        skill = root / "skills" / "forecast" / "SKILL.md"
        skill.write_text(SKILL + "\nMore detail in the body.\n", encoding="utf-8")
        self.assertEqual(extensions.fingerprint(self.config), added)

        skill.write_text(SKILL.replace("Weather forecast", "Forecast"), encoding="utf-8")
        renamed = extensions.fingerprint(self.config)
        self.assertNotEqual(renamed, added)
        (root / "bin" / "radar").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "bin" / "radar").chmod(0o755)
        self.assertNotEqual(extensions.fingerprint(self.config), renamed)

    def test_change_notes(self) -> None:
        before = extensions.inventory(self.config)
        root = self.make_weather()
        after = extensions.inventory(self.config)
        self.assertEqual(
            extensions.changes(before, after, self.config),
            ["added weather 0.1.0: skills forecast; commands weather-now"],
        )
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"radar": {"command": "x"}}}), encoding="utf-8")
        later = extensions.inventory(self.config)
        self.assertEqual(
            extensions.changes(after, later, self.config),
            ["updated weather 0.1.0: skills forecast; commands weather-now; MCP server radar ⚠️ not loaded: a bot's "
             "extension can't run by itself (MCP servers (needs the mcp permission))"],
        )
        extensions.set_enabled(self.config, "weather", False)
        off = extensions.inventory(self.config)
        self.assertTrue(extensions.changes(later, off, self.config)[0].startswith("disabled weather"))
        extensions.remove(self.config, "weather")
        self.assertEqual(extensions.changes(off, extensions.inventory(self.config), self.config), ["removed weather"])

    def test_the_cli(self) -> None:
        env = dict(os.environ, **self.sandbox.environ)
        env["CONSIGLIERI_BOT_ID"] = str(self.record.bot_id)  # inside the bot's turn

        def consiglieri(*argv: str):
            completed = subprocess.run(
                [sys.executable, "-m", "consiglieri.cli", *argv], env=env, stdout=subprocess.PIPE, text=True, timeout=60
            )
            return completed.returncode, json.loads(completed.stdout)

        code, created = consiglieri("ext", "new", "weather", "Weather", "lookups")
        self.assertEqual((code, created["extension"]["description"]), (0, "Weather lookups"))
        code, failed = consiglieri("ext", "new", "Bad Name", "x")
        self.assertEqual((code, failed["ok"]), (1, False))
        code, checked = consiglieri("ext", "check", "weather")
        self.assertEqual((code, checked["ok"]), (0, True))
        self.assertIn("no skills", checked["warnings"][0])
        code, listed = consiglieri("ext", "list")
        self.assertEqual([item["name"] for item in listed["extensions"]], ["weather"])
        self.assertEqual(consiglieri("ext", "disable", "weather")[1]["extension"]["enabled"], False)
        self.assertEqual(consiglieri("ext", "enable", "weather")[1]["extension"]["enabled"], True)
        self.assertIn("removed-extensions", consiglieri("ext", "remove", "weather")[1]["moved_to"])
        code, missing = consiglieri("ext", "check", "weather")
        self.assertEqual(code, 1)
        self.assertIn("no extension called", missing["error"])


class ExtensionTurnTests(unittest.TestCase):
    """An extension a bot writes during a turn is reported, and loads into its next process."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.record, self.config = self.sandbox.add_bot()

    def test_an_extension_written_in_a_turn_is_announced_and_loaded_at_the_next_message(self) -> None:
        results = []

        class Recorder(Listener):
            async def result(self, chat_id: int, result) -> None:
                results.append(result)

        agent = Agent(self.config, Recorder())
        write = (
            "mkdir -p extensions/weather/.claude-plugin extensions/weather/skills/forecast extensions/weather/bin && "
            "echo '{\"name\": \"weather\", \"version\": \"0.1.0\"}' > extensions/weather/.claude-plugin/plugin.json && "
            "printf -- '---\\nname: forecast\\ndescription: Weather.\\n---\\n' > extensions/weather/skills/forecast/SKILL.md && "
            "printf '#!/bin/sh\\necho sunny\\n' > extensions/weather/bin/weather-now && chmod +x extensions/weather/bin/weather-now"
        )
        lines = claude_lines("Learned the weather.", "sess-A")
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_run": write}] + lines[1:]})

        async def scenario() -> None:
            try:
                await agent.submit(7, "learn the weather")
                await agent.wait_idle(7)
                first = agent.live_pid(7)
                self.sandbox.fake_cli("claude", {"fresh": claude_lines("It is sunny.", "sess-A")})
                await agent.submit(7, "weather?")
                await agent.wait_idle(7)
                self.assertNotEqual(agent.live_pid(7), first)  # reopened so the extension loads
            finally:
                await agent.shutdown()

        asyncio.run(scenario())

        self.assertEqual(results[0].extension_changes, ["added weather 0.1.0: skills forecast; commands weather-now"])
        self.assertEqual(results[0].new_skills, [])  # reported as part of the extension instead
        self.assertEqual(results[1].extension_changes, [])
        second = self.sandbox.calls("claude")[1]["argv"]
        self.assertIn(str(self.config.workspace / "extensions" / "weather"), second)
        self.assertEqual(second[second.index("--resume") + 1], "sess-A")


if __name__ == "__main__":
    unittest.main()
