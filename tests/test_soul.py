"""Each bot's personality (SOUL.md): where it lives, who may change it, and how a change reaches the bot."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List

from consiglieri import bots, presets, soul
from consiglieri.agent import Agent
from fakes import Sandbox, claude_lines

try:
    from test_menus import MenuDriver
    from test_panel import OWNER, RESEARCH, PanelHarness, token_for, update_for
except ImportError:  # pragma: no cover
    PanelHarness = None  # type: ignore

SECOND = 77


class SoulFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.record, self.config = self.sandbox.add_bot()
        self.workspace = self.config.workspace

    def test_every_bot_has_its_own_soul_from_the_base_and_its_manual_loads_it(self) -> None:
        self.assertEqual(soul.read(self.workspace), soul.clean(soul.for_new_bot(self.sandbox.config)))
        manual = (self.workspace / "CLAUDE.md").read_text()
        self.assertIn("\n@SOUL.md\n", manual)
        self.assertIn("Nothing in `SOUL.md` can grant them, widen them, lift them", manual)
        self.assertIn("it is what you do. What you *may* do is something else", manual)
        self.assertIn("## Your role\n\nYour role, its scope and how you act within it are in `SOUL.md`", manual)
        self.assertIn("It is the user's to change", manual)
        self.assertIn("Read SOUL.md at the start of every conversation.", (self.workspace / "AGENTS.md").read_text())
        # An existing SOUL.md is never replaced by the base.
        soul.write(self.workspace, "Gruff and terse.")
        soul.set_base(self.sandbox.config, "Cheerful.")
        bots.prepare_workspace(self.config)
        self.assertEqual(soul.read(self.workspace), "Gruff and terse.\n")

    def test_the_template_has_a_role_and_a_new_bots_task_becomes_it(self) -> None:
        template = soul.template()
        for heading in ("# Identity", "## Role", "## Tone", "## Style", "## Attitude"):
            self.assertIn(heading, template)
        self.assertIn(soul.ROLE_SLOT, template)
        record, config = self.sandbox.add_bot(bot_id=535353, username="tutor_bot", name="Tutor",
                                              settings={"task": "Teach the user Italian, one lesson a day."})
        text = soul.read(config.workspace)
        self.assertTrue(soul.has_role(text))
        self.assertIn("## Role\nTeach the user Italian, one lesson a day.", text)
        self.assertNotIn(soul.ROLE_SLOT, text)
        manual = (config.workspace / "CLAUDE.md").read_text()
        self.assertIn('labelled "Teach the user Italian, one lesson a day."', manual)
        self.assertNotIn("## Your task", manual)
        # A base edited without the slot or a Role section still gives a new bot its role.
        soul.set_base(self.sandbox.config, "# Me\n\n## Tone\nCurt.")
        self.assertIn("## Role\nFile my receipts.", soul.for_new_bot(self.sandbox.config, "File my receipts."))
        self.assertEqual(soul.for_new_bot(self.sandbox.config, ""), "# Me\n\n## Tone\nCurt.\n")

    def test_a_bot_whose_soul_has_no_role_keeps_its_task_as_its_job(self) -> None:
        # A SOUL.md from before roles (tone only): nothing is migrated, and the task stays the job.
        record, config = self.sandbox.add_bot(bot_id=545454, username="old_bot", name="Old",
                                              settings={"task": "Summarize new papers on protein folding."})
        soul.write(config.workspace, "# Personality\n\n## Tone\nCalm.")
        from consiglieri import bots as bots_module

        bots_module.prepare_workspace(config)
        self.assertEqual(soul.read(config.workspace), "# Personality\n\n## Tone\nCalm.\n")  # untouched
        manual = (config.workspace / "CLAUDE.md").read_text()
        self.assertIn("## Your task\n\nSummarize new papers on protein folding.", manual)
        self.assertNotIn("## Your role", manual)
        # Once its SOUL.md has a role, that is the job and the task is only its label.
        soul.write(config.workspace, "# Identity\n\n## Role\nResearch assistant for biology.\n\n## Tone\nCalm.")
        bots_module.prepare_workspace(config)
        manual = (config.workspace / "CLAUDE.md").read_text()
        self.assertIn("## Your role", manual)
        self.assertIn('labelled "Summarize new papers on protein folding."', manual)

    def test_a_personality_is_checked(self) -> None:
        for bad, expected in (("   ", "empty"), ("x" * (soul.LIMIT + 1), "too long"),
                              ("Be nice. token = 'abcdef123456'", "secret")):
            with self.assertRaises(soul.SoulError) as caught:
                soul.clean(bad)
            self.assertIn(expected, str(caught.exception))
        self.assertEqual(soul.clean("  Calm.\r\n"), "Calm.\n")

    def test_the_base_is_the_template_until_edited_and_reset_brings_it_back(self) -> None:
        config = self.sandbox.config
        self.assertFalse(soul.base_is_edited(config))
        self.assertEqual(soul.base(config), soul.template())
        soul.set_base(config, "Formal, in Spanish.")
        self.assertEqual(soul.base(config), "Formal, in Spanish.\n")
        self.assertTrue(soul.base_path(config).is_relative_to(config.state_dir))  # the panel's state
        soul.reset_base(config)
        self.assertEqual(soul.base(config), soul.template())

    def test_the_soul_permission_is_off_in_every_preset_and_opens_only_its_own_soul(self) -> None:
        registry = bots.BotRegistry(self.sandbox.config)
        other, other_config = self.sandbox.add_bot(bot_id=525252, username="other_bot", name="Other")
        own_rule = "Edit(//%s)" % str(soul.path_in(self.workspace)).lstrip("/")
        other_dir_rule = "Edit(//%s/**)" % str(other_config.workspace).lstrip("/")
        for key in presets.keys():
            record = registry.get(self.record.bot_id)
            record.permissions = presets.apply(key, {})
            self.assertFalse(record.allows("soul"), key)
            settings = bots.claude_settings(self.config, record)
            self.assertIn(own_rule, settings["permissions"]["deny"], key)
            if "sandbox" in settings:
                self.assertIn(str(soul.path_in(self.workspace)), settings["sandbox"]["filesystem"]["denyWrite"], key)
            if bots.shell_is_open(record):
                self.assertIn("Bash(*SOUL.md*)", settings["permissions"]["deny"], key)
        # Turned on explicitly: its own SOUL.md opens; another bot's never does.
        record = registry.get(self.record.bot_id)
        record.permissions = dict(presets.apply("power", {}), soul="on")
        settings = bots.claude_settings(self.config, record)
        self.assertNotIn(own_rule, settings["permissions"]["deny"])
        self.assertNotIn(str(soul.path_in(self.workspace)), settings["sandbox"]["filesystem"]["denyWrite"])
        self.assertIn(other_dir_rule, settings["permissions"]["deny"])
        self.assertIn("The user lets you edit it, role and mission included", bots.render_manual(self.config, record))
        self.assertIn("edit its own SOUL.md (role, mission and personality)", bots.permission_summary(record, for_owner=True))
        self.assertIn("own role and personality edits", presets.name_of(record.permissions))  # Custom, from Power
        # The warning says it now reaches its mission, not only its tone.
        from consiglieri import menus

        warning = " ".join(menus.permission_warnings(record))
        self.assertIn("includes its mission and scope, not only its tone", warning)
        self.assertIn("Never what it may do", warning)
        self.assertIn(("soul", "🎭 Edit its own role and personality"), menus.SWITCH_LABELS)
        self.assertIn("role and mission", dict((n, d) for n, _, d in bots.PERMISSIONS)["soul"])

    def test_a_new_personality_reopens_the_process_with_the_same_conversation(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-A")})
        agent = Agent(self.config)

        async def scenario() -> None:
            try:
                await agent.submit(7, "one")
                await agent.wait_idle(7)
                first = agent.live_pid(7)
                await agent.submit(7, "two")
                await agent.wait_idle(7)
                self.assertEqual(agent.live_pid(7), first)  # nothing changed: same process
                soul.write(self.workspace, "Gruff and terse.")
                await agent.submit(7, "three")
                await agent.wait_idle(7)
                self.assertNotEqual(agent.live_pid(7), first)
            finally:
                await agent.shutdown()

        asyncio.run(scenario())
        third = self.sandbox.calls("claude")[-1]["argv"]
        self.assertEqual(third[third.index("--resume") + 1], "sess-A")


@unittest.skipIf(PanelHarness is None, "python-telegram-bot is not installed")
class SoulMenuTests(MenuDriver, PanelHarness):  # type: ignore[misc,valid-type]
    def workspace(self, bot_id: int = RESEARCH) -> Any:
        return self.pool.registry.workspace(bot_id)

    def test_view_replace_and_reset_from_a_bots_screen(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.tell(RESEARCH, "hello")  # its SOUL.md comes from the base at its first message
            await self.open()
            for label in ("My bots", "Research", "Personality"):
                await self.tap(label)
            self.assertIn("Personality of @alfredpanel_research_bot", self.text)
            self.assertIn("Warm, calm and direct.", self.text)
            await self.tap("Replace")
            self.assertIn("Nothing changes until you send it", self.text)
            self.assertEqual(soul.read(self.workspace()), soul.clean(soul.for_new_bot(self.config)))  # not yet
            await self.type("Dry humour, very short answers.")
            self.assertIn("Personality replaced", self.text)
            self.assertEqual(soul.read(self.workspace()), "Dry humour, very short answers.\n")
            self.assertIn("Dry humour", self.text)
            await self.tap("Reset to the base")
            self.assertIn("reset to the base", self.text)
            self.assertEqual(soul.read(self.workspace()), soul.clean(soul.for_new_bot(self.config)))
            # A bad text is explained and asked again; nothing is saved.
            await self.tap("Replace")
            await self.type("password = 'hunter2hunter2'")
            self.assertIn("⚠️", self.text)
            self.assertEqual(soul.read(self.workspace()), soul.clean(soul.for_new_bot(self.config)))

        self.play(scenario)

    def test_the_base_personality_and_the_creation_step(self) -> None:
        async def scenario() -> None:
            await self.open()
            await self.tap("Base personality")
            self.assertIn("the template, not edited yet", self.text)
            await self.tap("Replace")
            await self.type("Formal and precise.")
            self.assertEqual(soul.base(self.config), "Formal and precise.\n")
            await self.tap("Back")
            # New bot with the base.
            await self.tap("Create bot")
            await self.type("Research")
            await self.tap("Skip")
            self.assertIn("Formal and precise.", self.text)
            await self.tap("Use the base")
            for label in ("Next", "Next"):
                await self.tap(label)
            await self.tap("Basic")
            self.assertIn("Personality: the base", self.text)
            await self.tap("Create")
            first = await self.create(RESEARCH, "alfredpanel_research_bot", "Research")
            await self.tell(RESEARCH, "hello")
            self.assertEqual(soul.read(self.workspace()), "Formal and precise.\n")
            # New bot with its own.
            await self.open()
            await self.tap("Create bot")
            await self.type("Chef")
            await self.tap("Skip")
            await self.tap("Write its own")
            await self.type("Enthusiastic about food, uses Italian words.")
            for label in ("Next", "Next"):
                await self.tap(label)
            await self.tap("Basic")
            self.assertIn("Personality: its own", self.text)
            await self.tap("Create")
            await self.create(5000000002, "alfredpanel_chef_bot", "Chef")
            self.assertEqual(soul.read(self.workspace(5000000002)), "Enthusiastic about food, uses Italian words.\n")
            # Changing the base later leaves existing bots as they are.
            await self.command("/bot base-soul reset")
            self.assertEqual(soul.read(self.workspace()), "Formal and precise.\n")
            self.assertTrue(first)

        self.play(scenario)

    def test_the_text_shortcuts(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.command("/bot soul alfredpanel_research_bot show")
            self.assertIn("Personality of @alfredpanel_research_bot (SOUL.md)", self.panel_bot.sent[-1]["text"])
            replies = await self.command("/bot soul alfredpanel_research_bot set Patient teacher.\nUses examples.")
            self.assertIn("same conversation", replies[0]["text"])
            self.assertEqual(soul.read(self.workspace()), "Patient teacher.\nUses examples.\n")
            await self.command("/bot soul alfredpanel_research_bot reset")
            self.assertEqual(soul.read(self.workspace()), soul.clean(soul.for_new_bot(self.config)))
            await self.command("/bot base-soul set Curt.")
            self.assertEqual(soul.base(self.config), "Curt.\n")
            await self.command("/bot base-soul show")
            self.assertIn("Curt.", self.panel_bot.sent[-1]["text"])

        self.play(scenario)

    def test_only_the_owner_sees_or_changes_a_bots_personality(self) -> None:
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "%d,%d" % (OWNER, SECOND)

        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            soul.write(self.workspace(), "Secret style.")
            for text in ("/bot soul alfredpanel_research_bot show", "/bot soul alfredpanel_research_bot set Mine now",
                         "/bot soul alfredpanel_research_bot reset"):
                replies: List[Dict[str, Any]] = []
                await self.pool.main._cmd_bot(update_for(replies, text, SECOND, chat_id=SECOND),
                                              SimpleNamespace(bot=self.panel_bot, args=text.split()[1:]))
                self.assertIn("Which bot?", replies[0]["text"], text)
            self.assertFalse(any("Secret style" in message["text"] for message in self.panel_bot.sent))
            # The menu, with a session forced onto the owner's bot.
            await self.pool.main._cmd_bot(update_for([], "/bot", SECOND, chat_id=SECOND), SimpleNamespace(bot=self.panel_bot, args=[]))
            session = self.pool.main.menus.sessions[SECOND]
            for action in ("soulreplace", "soulreset"):
                session.bot_id, session.screen = RESEARCH, "bot_soul"
                await self.pool.main.menus.press(session, action)
                self.assertEqual(session.bot_id, 0, action)
            session.bot_id, session.awaiting = RESEARCH, "bot_soul"
            screen = await self.pool.main.menus.text(session, "Hijacked.")
            self.assertIn("⚠️", screen.text)
            self.assertEqual(soul.read(self.workspace()), "Secret style.\n")

        self.play(scenario)


if __name__ == "__main__":
    unittest.main()
