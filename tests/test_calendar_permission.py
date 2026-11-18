"""The `calendar` permission: on by default and in every preset, enforced by Claude Code's rules whatever
`mcp` and the mode say, shown by the panel, and never granted by a bot admin without the user's yes."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict

from alfred import bot_admin, bots, home, menus, presets
from alfred.bots import BotRecord, BotRegistry
from fakes import Sandbox

try:
    from alfred.telegram_bot import BotPool  # noqa: F401  (test_panel needs it to import)
    from test_panel import OWNER, RESEARCH, PanelHarness
except ImportError:  # pragma: no cover
    PanelHarness = unittest.TestCase  # type: ignore
    OWNER = RESEARCH = 0

BOT = 5000000001
CALENDAR = "mcp__claude_ai_Google_Calendar__*"
SKILL = Path(__file__).resolve().parent.parent / "alfred" / "plugin" / "skills" / "calendar" / "SKILL.md"


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "1:PANELPANELPANELPANELPANELPANELPANEL"
        home.prepare(self.sandbox.config)
        self.registry = BotRegistry(self.sandbox.config)

    def bot(self, **permissions: str) -> BotRecord:
        self.registry.save(BotRecord(bot_id=BOT, username="agenda_bot", name="Agenda", owner_id=99,
                                     chat_ids=[99], permissions=dict(permissions)))
        return self.registry.get(BOT)

    def rules(self, **permissions: str) -> Dict[str, Any]:
        return bots.claude_settings(self.registry.config_for(BOT), self.bot(**permissions))["permissions"]

    def test_on_by_default_and_in_every_preset_and_template(self) -> None:
        self.assertEqual(bots.DEFAULT_PERMISSIONS["calendar"], "on")
        self.assertIn("calendar", bots.SWITCHES)
        self.assertTrue(self.bot().allows("calendar"))
        for key in presets.keys():  # Basic included
            self.assertEqual(presets.resolve(key)["calendar"], "on", key)
        for key in presets.templates():
            self.assertEqual(presets.resolve(key)["calendar"], "on", key)

    def test_values_and_options(self) -> None:
        config = self.sandbox.config
        self.assertEqual(bots.clean_permission(config, "calendar", "yes"), "on")
        self.assertEqual(bots.clean_permission(config, "calendar", "default"), "on")
        with self.assertRaises(bots.BotError):
            bots.clean_permission(config, "calendar", "read")
        _, permissions, rest = bots.parse_options(config, ["+calendar", "Agenda"])
        self.assertEqual((permissions, rest), ({"calendar": "on"}, ["Agenda"]))

    def test_denied_without_it_in_every_preset_even_with_mcp_on(self) -> None:
        for key in presets.keys():
            with self.subTest(key):
                rules = self.rules(**dict(presets.resolve(key), calendar="off"))
                self.assertIn(CALENDAR, rules["deny"])  # deny wins over bypassPermissions and the mcp switch
                self.assertNotIn(CALENDAR, rules["allow"])

    def test_allowed_with_it_and_not_removed_by_mcp_off(self) -> None:
        rules = self.rules(calendar="on")
        self.assertIn(CALENDAR, rules["allow"])
        self.assertNotIn(CALENDAR, rules["deny"])
        self.assertNotIn("mcp__*", rules["deny"])  # the blanket deny would remove the connector too
        self.assertIn("mcp__*", self.rules(calendar="off")["deny"])  # without it, mcp off still removes every MCP tool
        rules = self.rules(calendar="on", mcp="on")
        self.assertIn(CALENDAR, rules["allow"])
        self.assertNotIn(CALENDAR, rules["deny"])

    def test_summary_names_it(self) -> None:
        phrase = "read and create events in the user's Google Calendar"
        self.assertIn(phrase, bots.permission_summary(self.bot(calendar="off")).split("You cannot")[1])
        self.assertIn(phrase, bots.permission_summary(self.bot(calendar="on")).split("You cannot")[0])
        self.assertIn("calendar: off", bots.describe(self.bot(calendar="off")))
        self.assertIn("calendar: on", bots.describe(self.bot()))

    def test_panel_warns_about_the_connector_and_codex(self) -> None:
        self.assertFalse(any("Calendar" in w for w in menus.permission_warnings(self.bot(calendar="off"))))
        self.assertIn(menus.CALENDAR_WARNING, menus.permission_warnings(self.bot(calendar="on")))
        codex = menus.permission_warnings(self.bot(calendar="on"), "codex")
        self.assertTrue(any("Codex can't use the Google Calendar connector" in w for w in codex))

    def test_a_bot_admin_gives_it_without_a_separate_yes(self) -> None:
        # A default, like reminders: it is not one of the extras the user approves on a separate button.
        self.assertNotIn("calendar", bot_admin.SENSITIVE_SWITCHES)
        self.assertNotIn("calendar", bot_admin.EXTRAS)
        self.assertEqual(bot_admin.extras_of({"calendar": "on"}, False), [])

    def test_the_skill_asks_before_creating(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("name: calendar", text)
        self.assertIn("mcp__claude_ai_Google_Calendar__", text)
        self.assertIn("only when the user asked for that event", text)


class BotAdminSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config
        self.admin, _ = self.sandbox.add_bot(bot_id=5000000100, username="boss_bot", name="Boss",
                                             permissions=presets.apply("botadmin", {}))

    def test_refused_above_the_admin_and_given_within_it(self) -> None:
        self.admin.permissions["calendar"] = "off"
        BotRegistry(self.config).save(self.admin)
        admin = BotRegistry(self.config).get(self.admin.bot_id)
        with self.assertRaises(bot_admin.SpecError) as caught:
            bot_admin.parse_spec(self.config, admin, {"name": "Agenda", "permissions": {"calendar": "on"}})
        self.assertIn("calendar on is more than @boss_bot has", "; ".join(caught.exception.problems))
        admin.permissions["calendar"] = "on"
        BotRegistry(self.config).save(admin)
        admin = BotRegistry(self.config).get(self.admin.bot_id)
        checked = bot_admin.parse_spec(self.config, admin, {"name": "Agenda", "permissions": {"calendar": "on"}})
        self.assertEqual(checked.extras, [])
        self.assertTrue(BotRecord(bot_id=0, username="", name="", owner_id=0,
                                  permissions=dict(checked.initial)).allows("calendar"))  # it starts with it


@unittest.skipIf(OWNER == 0, "python-telegram-bot is not installed")
class PanelTests(PanelHarness):  # type: ignore[misc,valid-type]
    def test_bot_perm_switches_it_and_the_menu_toggles_it(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("calendar"), "on")  # on from the start
            await self.command("/bot perm research_bot calendar off")
            text = (await self.command("/bot perm research_bot calendar on"))[-1]["text"]
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("calendar"), "on")
            self.assertIn("claude.ai → Settings → Connectors", text)
            await self.command("/bot perm research_bot calendar off")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("calendar"), "off")

            panel_menus = self.pool.main.menus
            session, _ = panel_menus.open(OWNER, OWNER)
            await panel_menus.press(session, "bot:%d" % RESEARCH)
            await panel_menus.press(session, "go:bot_perms")
            screen = await panel_menus.press(session, "custom")
            self.assertIn("⬜ 📅 Read and create calendar events", [label for row in screen.rows for label, _ in row])
            screen = await panel_menus.press(session, "perm:calendar")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("calendar"), "on")
            self.assertIn("✅ 📅 Read and create calendar events", [label for row in screen.rows for label, _ in row])
            self.assertIn("claude.ai → Settings → Connectors", screen.text)
            screen = await panel_menus.press(session, "perm:calendar")  # off by hand
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("calendar"), "off")
            await panel_menus.press(session, "back")
            await panel_menus.press(session, "back")
            await panel_menus.press(session, "go:bot_perms")
            screen = await panel_menus.press(session, "pick:basic")  # picking a preset puts it back on, its default
            self.assertIn("+ calendar", screen.text)  # it gains it, so the panel asks first
            await panel_menus.press(session, "pickok")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("calendar"), "on")

        self.play(scenario)


if __name__ == "__main__":
    unittest.main()
