"""The `usage` permission: off by default and in every preset, shown by the panel, and enforced."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Tuple
from unittest import mock

from alfred import bots, cli, home, presets
from alfred.bots import BotRecord, BotRegistry
from fakes import Sandbox

try:
    from alfred.telegram_bot import BotPool  # noqa: F401  (test_panel needs it to import)
    from test_panel import OWNER, RESEARCH, PanelHarness
except ImportError:  # pragma: no cover
    PanelHarness = unittest.TestCase  # type: ignore
    OWNER = RESEARCH = 0

REPORT = "Current session: 33% used · resets 12:40pm (Europe/Berlin)\n"
BOT = 5000000001


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        home.prepare(self.sandbox.config)
        self.registry = BotRegistry(self.sandbox.config)

    def bot(self, **permissions: str) -> BotRecord:
        self.registry.save(BotRecord(bot_id=BOT, username="research_bot", name="Research", owner_id=99,
                                     chat_ids=[99], permissions=dict(permissions)))
        return self.registry.get(BOT)

    def settings(self, **permissions: str) -> Dict[str, Any]:
        return bots.claude_settings(self.registry.config_for(BOT), self.bot(**permissions))

    def test_off_by_default_and_in_every_preset(self) -> None:
        self.assertEqual(bots.DEFAULT_PERMISSIONS["usage"], "off")
        self.assertIn("usage", bots.SWITCHES)
        self.assertFalse(self.bot().allows("usage"))
        for key in presets.keys():  # the security presets (alfred/presets.py), Full access included
            self.assertEqual(presets.resolve(key)["usage"], "off", key)

    def test_values_and_options(self) -> None:
        config = self.sandbox.config
        self.assertEqual(bots.clean_permission(config, "usage", "yes"), "on")
        self.assertEqual(bots.clean_permission(config, "usage", "default"), "off")
        with self.assertRaises(bots.BotError):
            bots.clean_permission(config, "usage", "maybe")
        _, permissions, rest = bots.parse_options(config, ["+usage", "Research"])
        self.assertEqual((permissions, rest), ({"usage": "on"}, ["Research"]))

    def test_claude_code_denies_the_usage_commands_without_it(self) -> None:
        deny = self.settings()["permissions"]["deny"]
        for rule in bots.USAGE_RULES:
            self.assertIn(rule, deny)
        deny = self.settings(usage="on")["permissions"]["deny"]
        for rule in bots.USAGE_RULES:
            self.assertNotIn(rule, deny)

    def test_sandbox_lets_the_usage_commands_out_only_with_it(self) -> None:
        excluded = self.settings(shell="full")["sandbox"]["excludedCommands"]
        self.assertNotIn("alfred usage-alert *", excluded)
        excluded = self.settings(shell="full", usage="on")["sandbox"]["excludedCommands"]
        self.assertIn("alfred usage-alert *", excluded)
        self.assertIn("alfred usage", excluded)

    def test_summary_names_it(self) -> None:
        self.assertIn("read the Claude plan usage", bots.permission_summary(self.bot()).split("You cannot")[1])
        self.assertIn("read the Claude plan usage", bots.permission_summary(self.bot(usage="on")).split("You cannot")[0])
        self.assertIn("usage: off", bots.describe(self.bot()))


class UsageCommandTests(unittest.TestCase):
    """`alfred usage` from inside a bot's turn: the permission and the global switch both have to be on."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        home.prepare(self.sandbox.config)
        self.sandbox.fake_cli("claude", {"fresh": [{"type": "result", "is_error": False, "result": REPORT}]})
        self.registry = BotRegistry(self.sandbox.config)
        self.registry.save(BotRecord(bot_id=BOT, username="research_bot", name="Research", owner_id=99, chat_ids=[99]))

    def run_usage(self, bot: bool = True) -> Tuple[int, str, str]:
        environ = dict(self.sandbox.environ, ALFRED_BOT_ID=str(BOT) if bot else "")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["usage"])
        return code, out.getvalue(), err.getvalue()

    def grant(self, value: str) -> None:
        record = self.registry.get(BOT)
        record.permissions["usage"] = value
        self.registry.save(record)

    def test_refused_without_the_permission(self) -> None:
        code, out, err = self.run_usage()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("usage permission", err)
        self.assertEqual(self.sandbox.calls("claude"), [])

    def test_the_permission_has_no_effect_while_the_monitor_is_off(self) -> None:
        self.grant("on")
        code, _, err = self.run_usage()
        self.assertEqual(code, 1)
        self.assertIn("usage monitor is off", err)
        self.assertEqual(self.sandbox.calls("claude"), [])

    def test_allowed_with_the_permission_and_the_monitor_on(self) -> None:
        self.grant("on")
        self.sandbox.environ["ALFRED_USAGE_MONITOR"] = "1"
        code, out, _ = self.run_usage()
        self.assertEqual(code, 0)
        self.assertIn("Session: 33% used", out)

    def test_the_operator_is_not_affected(self) -> None:
        code, out, _ = self.run_usage(bot=False)
        self.assertEqual(code, 0)
        self.assertIn("Session: 33% used", out)


@unittest.skipIf(OWNER == 0, "python-telegram-bot is not installed")
class PanelTests(PanelHarness):  # type: ignore[misc,valid-type]
    async def perm(self, value: str) -> str:
        replies = await self.command("/bot perm research_bot usage %s" % value)
        return replies[-1]["text"]

    def test_bot_perm_switches_it_and_warns_while_the_monitor_is_off(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            text = await self.perm("on")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("usage"), "on")
            self.assertIn("usage monitor is off", text)
            text = await self.perm("off")
            self.assertNotIn("usage monitor is off", text)

        self.play(scenario)

    def test_no_warning_when_the_monitor_is_on(self) -> None:
        self.sandbox.environ["ALFRED_USAGE_MONITOR"] = "1"

        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            self.assertNotIn("usage monitor is off", await self.perm("on"))

        self.play(scenario)

    def test_menu_selector_toggles_it(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            panel_menus = self.pool.main.menus
            session, _ = panel_menus.open(OWNER, OWNER)
            await panel_menus.press(session, "bot:%d" % RESEARCH)
            await panel_menus.press(session, "go:bot_perms")
            screen = await panel_menus.press(session, "custom")  # the switches are behind Customize
            labels = [label for row in screen.rows for label, _ in row]
            self.assertIn("⬜ 📊 Plan usage and alerts", labels)
            screen = await panel_menus.press(session, "perm:usage")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("usage"), "on")
            labels = [label for row in screen.rows for label, _ in row]
            self.assertIn("✅ 📊 Plan usage and alerts", labels)
            self.assertIn("usage monitor is off", screen.text)
            self.assertIn("Custom (from 🔒 Basic: + plan usage)", screen.text)

            # Its page keeps the warning; picking a preset turns the permission off again.
            screen = await panel_menus.press(session, "back")
            screen = await panel_menus.press(session, "back")
            self.assertIn("usage monitor is off", screen.text)
            await panel_menus.press(session, "go:bot_perms")
            await panel_menus.press(session, "pick:basic")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("usage"), "off")

            # The creation assistant has the switch under Customize too, off until tapped, and no preset sets it.
            session, _ = panel_menus.open(OWNER, OWNER)
            await panel_menus.press(session, "go:new_perms")
            screen = await panel_menus.press(session, "custom")
            self.assertIn("⬜ 📊 Plan usage and alerts", [label for row in screen.rows for label, _ in row])
            await panel_menus.press(session, "back")
            await panel_menus.press(session, "pick:full")
            self.assertEqual(session.permissions.get("usage", "off"), "off")

        self.play(scenario)

    def test_show_names_the_preset_and_keeps_the_warning(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            await self.perm("on")
            text = (await self.command("/bot show research_bot"))[-1]["text"]
            self.assertIn("Preset: 🎛 Custom (from 🔒 Basic: + plan usage)", text)
            self.assertIn("usage monitor is off", text)

        self.play(scenario)


if __name__ == "__main__":
    unittest.main()
