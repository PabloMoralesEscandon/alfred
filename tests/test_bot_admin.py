"""Bot admins (docs/BOT_ADMIN.md): bots that create other bots for their owner, never above themselves.

With a stand-in Telegram and a stand-in Claude Code: no network, no real bots, no credentials.
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict
from unittest import mock

from alfred import bots, presets
from alfred.bots import BotRecord, BotRegistry
from fakes import Sandbox

try:
    from test_menus import MenuDriver
    import test_panel
    from test_panel import OWNER, RESEARCH, STRANGER, PanelHarness
except ImportError:  # pragma: no cover - python-telegram-bot missing
    PanelHarness = None  # type: ignore

ADMIN_RULES = ["Bash(alfred bots %s *)" % name for name in bots.ADMIN_SUBCOMMANDS]
BOTS_RULES = ["Bash(alfred bots %s *)" % name for name in bots.BOTS_SUBCOMMANDS]


def rules_of(sandbox: Sandbox, **permissions: str) -> Dict[str, Any]:
    record, config = sandbox.add_bot(permissions=dict(permissions))
    return bots.claude_settings(config, record)


class AdminPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def test_the_admin_permission_is_off_in_every_preset_and_only_the_template_sets_it(self) -> None:
        self.assertEqual(bots.DEFAULT_PERMISSIONS["admin"], "off")
        for key in presets.keys():
            self.assertEqual(presets.resolve(key)["admin"], "off", key)  # Full access included
        self.assertEqual(presets.resolve("botadmin"), presets.values({"web": "on", "edit": "on", "admin": "on"}))
        self.assertNotIn("botadmin", presets.keys())  # not part of the order from least to most
        self.assertEqual(presets.detect({"web": "on", "edit": "on", "admin": "on"}), ("botadmin", "assistant"))
        self.assertEqual(presets.name_of({"web": "on", "edit": "on", "admin": "on"}), "🧑‍💼 Bot admin")
        self.assertEqual(presets.name_of({"admin": "on"}), "🎛 Custom (from 🔒 Basic: + bot admin)")
        self.assertEqual(presets.find("Bot admin"), "botadmin")
        self.assertEqual(presets.find("admin"), None)  # the word is the permission's, not a preset's
        self.assertIn("⚠️", presets.card("botadmin"))
        self.assertIn("+ bot admin", presets.gains({"web": "on", "edit": "on"}, presets.resolve("botadmin")))

    def test_claude_code_rules_close_each_helper_without_its_permission(self) -> None:
        none = rules_of(self.sandbox)["permissions"]["deny"]
        self.assertIn("Bash(alfred bots *)", none)

        only_bots = rules_of(self.sandbox, bots="on")["permissions"]["deny"]
        self.assertNotIn("Bash(alfred bots *)", only_bots)
        for rule in ADMIN_RULES:
            self.assertIn(rule, only_bots)
            self.assertIn(rule[:-3] + ")", only_bots)  # the bare form too: `alfred bots status`
        for rule in BOTS_RULES:
            self.assertNotIn(rule, only_bots)

        only_admin = rules_of(self.sandbox, admin="on")["permissions"]["deny"]
        self.assertNotIn("Bash(alfred bots *)", only_admin)
        for rule in ADMIN_RULES:
            self.assertNotIn(rule, only_admin)
        for rule in BOTS_RULES:
            self.assertIn(rule, only_admin)

        both = rules_of(self.sandbox, admin="on", bots="on")["permissions"]["deny"]
        self.assertFalse([rule for rule in both if rule.startswith("Bash(alfred bots")])

    def test_with_a_sandboxed_shell_the_helpers_run_outside_it_only_with_a_permission(self) -> None:
        sandboxed = rules_of(self.sandbox, shell="full")["sandbox"]["excludedCommands"]
        self.assertNotIn("alfred bots *", sandboxed)
        admin = rules_of(self.sandbox, shell="full", admin="on")["sandbox"]["excludedCommands"]
        self.assertIn("alfred bots *", admin)

    def test_the_manual_says_what_a_bot_admin_may_do(self) -> None:
        record, config = self.sandbox.add_bot(permissions={"admin": "on"})
        self.assertIn("create and set up bots for your owner", bots.permission_summary(record))
        plain, _ = self.sandbox.add_bot(bot_id=424243, username="other_bot")
        self.assertIn("create and set up bots", bots.permission_summary(plain).split("cannot", 1)[1])

    def test_each_bot_gets_a_key_no_other_bot_can_work_out(self) -> None:
        config = self.sandbox.config
        self.assertEqual(bots.bot_key(config, 1), "")  # the command never creates the secret
        first = bots.bot_key(config, 1, create=True)
        self.assertEqual(len(first), 40)
        self.assertEqual(bots.bot_key(config, 1), first)
        self.assertNotEqual(bots.bot_key(config, 2), first)
        self.assertTrue(bots.key_matches(config, 1, first))
        self.assertFalse(bots.key_matches(config, 2, first))
        self.assertFalse(bots.key_matches(config, 1, ""))
        secret = config.state_dir / bots.KEY_FILE
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        # The secret lives in the home, which every bot is denied, at every level.
        record, bot_config = self.sandbox.add_bot(permissions={"shell": "full"})
        settings = bots.claude_settings(bot_config, record)
        self.assertIn("Read(//%s/**)" % str(config.home).lstrip("/"), settings["permissions"]["deny"])
        self.assertIn(str(config.home), settings["sandbox"]["filesystem"]["denyRead"])


@unittest.skipIf(PanelHarness is None, "python-telegram-bot is not installed")
class AdminPermissionPanelTests(MenuDriver, PanelHarness):  # type: ignore[misc,valid-type]
    button = test_panel.PanelTests.button

    def test_becoming_a_bot_admin_asks_first_by_text_and_is_audited(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            asked = await self.command("/bot perm research_bot admin on")
            self.assertIn("⚠️ Bot admin for @research_bot?", asked[0]["text"])
            self.assertFalse(self.pool.registry.get(RESEARCH).allows("admin"))  # nothing before the answer
            grant, cancel = asked[0]["reply_markup"].inline_keyboard[0]
            stranger = self.button(grant.callback_data, user_id=STRANGER)
            await self.pool.main._on_button(stranger, mock.Mock(bot=self.panel_bot))
            self.assertFalse(self.pool.registry.get(RESEARCH).allows("admin"))
            pressed = self.button(grant.callback_data)
            await self.pool.main._on_button(pressed, mock.Mock(bot=self.panel_bot))
            self.assertTrue(self.pool.registry.get(RESEARCH).allows("admin"))
            self.assertIn("is now a bot admin", pressed.edits[-1])

            # The template by text asks too, and a preset that drops it applies at once.
            await self.command("/bot perm research_bot basic")
            self.assertFalse(self.pool.registry.get(RESEARCH).allows("admin"))
            asked = await self.command("/bot perm research_bot botadmin")
            self.assertIn("It would have: 🧑‍💼 Bot admin", asked[0]["text"])
            await self.pool.main._on_button(self.button(asked[0]["reply_markup"].inline_keyboard[0][0].callback_data),
                                            mock.Mock(bot=self.panel_bot))
            self.assertEqual(presets.name_of(self.pool.registry.get(RESEARCH).permissions), "🧑‍💼 Bot admin")
            events = [entry["event"] for entry in bots.read_audit(self.config)]
            self.assertEqual(events, ["admin_permission_granted", "admin_permission_revoked", "admin_permission_granted"])
            audit = await self.command("/bot audit")
            self.assertIn("bot admin permission given to @research_bot by %d" % OWNER, audit[0]["text"])

        self.play(scenario)

    def test_becoming_a_bot_admin_from_the_menu_goes_through_the_warning(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            await self.tap("Permissions")
            await self.tap("🎛 Customize")
            await self.tap("Create bots for you (bot admin)")
            self.assertIn("⚠️ Bot admin for @research_bot?", self.text)
            await self.tap("No, back")
            self.assertFalse(self.pool.registry.get(RESEARCH).allows("admin"))
            await self.tap("🎛 Customize")
            await self.tap("Create bots for you (bot admin)")
            await self.tap("Yes, make it a bot admin")
            self.assertTrue(self.pool.registry.get(RESEARCH).allows("admin"))
            self.assertIn("✅ 🧑‍💼 Create bots for you (bot admin)", self.labels())
            self.assertIn("Bot admin: it can send you requests", self.text)
            await self.tap("Create bots for you (bot admin)")  # taking it away needs no question
            self.assertFalse(self.pool.registry.get(RESEARCH).allows("admin"))

            await self.tap("Back")
            await self.tap("Bot admin")  # the template
            self.assertIn("It gains: + file edits, + web, + bot admin", self.text)
            self.assertIn("⚠️ Bot admin for @research_bot?", self.text)
            await self.tap("Yes, switch to Bot admin")
            self.assertIn("● 🧑‍💼 Bot admin", self.labels())
            self.assertEqual([entry["via"] for entry in bots.read_audit(self.config)], ["menu", "menu", "menu"])

        self.play(scenario)


if __name__ == "__main__":
    unittest.main()
