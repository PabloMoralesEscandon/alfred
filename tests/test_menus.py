"""The /bot menus, driven like a user would: open /bot, tap buttons by their label, type answers."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

try:
    from test_panel import CHEF as CHEF_ID, OWNER, RESEARCH, STRANGER, PanelHarness, update_for
except ImportError:  # pragma: no cover
    PanelHarness = None  # type: ignore

from alfred import bots, keepass, menus, presets, scheduler

HAS_CLI = bool(keepass._find([str(keepass.MAC_APP / "keepassxc-cli"), "keepassxc-cli"]))
TOTP_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 SHA1 test key


class MenuDriver:
    """Open /bot, tap buttons by their label, type answers. Mixed into a PanelHarness; no tests of its own."""

    async def open(self) -> None:
        replies = await self.command("/bot")
        self.text, self.rows = replies[0]["text"], self._rows(replies[0]["reply_markup"])
        self.message_id = 1

    @staticmethod
    def _rows(markup: Any) -> List[List[Tuple[str, str]]]:
        if markup is None:
            return []
        return [[(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard]

    def labels(self) -> List[str]:
        return [label for row in self.rows for label, _ in row]

    def data(self, label: str) -> str:
        exact = [data for row in self.rows for text, data in row if text.lstrip("● ") == label]
        matches = exact or [data for row in self.rows for text, data in row if label in text]
        assert len(matches) == 1, "%r matches %d buttons in %s" % (label, len(matches), self.labels())
        return matches[0]

    async def tap(self, label: str, user_id: int = OWNER, data: Optional[str] = None) -> Optional[str]:
        """Press a button. Returns the toast text, if Telegram would show one."""
        data = data or self.data(label)
        toast: List[Optional[str]] = []

        async def answer(text: Optional[str] = None) -> None:
            toast.append(text)

        async def edit_message_text(text: str, reply_markup: Any = None) -> None:
            self.text, self.rows = text, self._rows(reply_markup)

        update = update_for([], "", user_id)
        update.callback_query = SimpleNamespace(
            data=data, answer=answer, edit_message_text=edit_message_text, message=SimpleNamespace(message_id=self.message_id)
        )
        await self.pool.main._on_button(update, SimpleNamespace(bot=self.panel_bot))
        return toast[0] if toast else None

    async def type(self, text: str) -> None:
        before = len(self.panel_bot.edits)
        replies: List[Dict[str, Any]] = []
        await self.pool.main._on_message(update_for(replies, text), SimpleNamespace(bot=self.panel_bot, args=[]))
        if len(self.panel_bot.edits) > before:
            edit = self.panel_bot.edits[-1]
            self.text, self.rows = edit["text"], self._rows(edit["reply_markup"])
        else:
            self.replies = replies

    async def walk_to_summary(self) -> None:
        await self.open()
        await self.tap("Create bot")
        await self.type("Gestor de Calendarios")
        await self.type("Keep my calendar tidy and remind me of events.")
        await self.tap("Use the base")
        await self.tap("Sonnet")
        await self.tap("high")
        await self.tap("Customize")  # starts from the recommended preset, Assistant
        await self.tap("Browser")
        await self.tap("Next")


@unittest.skipIf(PanelHarness is None, "python-telegram-bot is not installed")
class MenuTests(MenuDriver, PanelHarness):  # type: ignore[misc,valid-type]
    # -- the assistant --

    def test_menu_the_assistant_walks_through_every_step_and_creates_the_request(self) -> None:
        async def scenario() -> None:
            await self.open()
            self.assertEqual([label for label in self.labels()], ["➕ Create bot", "🤖 My bots", "🧠 Shared memory", "📜 Audit", "🎭 Base personality", "❓ Help", "✖️ Close"])
            await self.tap("Create bot")
            self.assertIn("Step 1 of 7: name", self.text)
            await self.type("Gestor de Calendarios")
            self.assertIn("Step 2 of 7: task", self.text)
            await self.type("Keep my calendar tidy and remind me of events.")
            self.assertIn("Step 3 of 7: personality", self.text)
            self.assertIn("starts with the base personality", self.text)
            await self.tap("Use the base")
            self.assertIn("Step 4 of 7: model", self.text)
            await self.tap("Sonnet")
            self.assertIn("Step 5 of 7: effort", self.text)
            await self.tap("high")
            self.assertIn("Step 6 of 7: permissions", self.text)
            self.assertIn("● ⭐ Assistant (recommended)", self.labels())
            await self.tap("Customize")
            self.assertIn("✅ 🌐 Web search and pages", self.labels())
            self.assertIn("✅ 📁 Edit its working folder", self.labels())
            await self.tap("Browser")
            self.assertIn("⚠️ Browser", self.text)  # the risky choice is flagged
            await self.tap("Next")
            self.assertIn("Step 7 of 7", self.text)
            # AlfredPanelBot -> alfredpanel_ + slug, cut to fit 32 characters.
            self.assertIn("Username: @alfredpanel_gestor_de_calend_bot", self.text)
            self.assertIn("Model: sonnet", self.text)
            self.assertIn("Effort: high", self.text)
            self.assertIn("Permissions: 🎛 Custom (from ⭐ Assistant: + browser)", self.text)
            self.assertIn("It can", self.text)

            await self.tap("Create")
            self.assertEqual(self.rows, [])
            self.assertIn("Request sent", self.text)
            keyboard = self.panel_bot.sent[-2]["reply_markup"].to_dict()
            request = keyboard["keyboard"][0][0]["request_managed_bot"]
            self.assertEqual(request["suggested_username"], "alfredpanel_gestor_de_calend_bot")
            self.assertEqual(request["suggested_name"], "Gestor de Calendarios")
            self.assertIn("t.me/newbot/AlfredPanelBot/alfredpanel_gestor_de_calend_bot", self.panel_bot.sent[-1]["text"])

            # Telegram creates it; the bot starts with exactly what was chosen.
            self.panel_bot.tokens[RESEARCH] = "%d:%s" % (RESEARCH, "A" * 35)
            record = await self.pool.on_managed_bot(
                {"id": OWNER}, {"id": RESEARCH, "username": "alfredpanel_gestor_de_calend_bot", "first_name": "Gestor de Calendarios"})
            self.assertEqual(record.settings, {"task": "Keep my calendar tidy and remind me of events.",
                                               "model": "sonnet", "effort": "high"})
            self.assertEqual(record.permissions, {"web": "on", "edit": "on", "browser": "on"})
            self.assertIn(RESEARCH, self.pool.frontends)

        self.play(scenario)

    def test_menu_back_returns_step_by_step_keeping_what_was_entered(self) -> None:
        async def scenario() -> None:
            await self.open()
            await self.tap("Create bot")
            await self.type("Research")
            await self.type("Summarize papers")
            await self.tap("Use the base")
            await self.tap("Opus")
            await self.tap("Back")
            self.assertIn("Step 4 of 7: model", self.text)
            self.assertIn("● Opus", self.labels())
            await self.tap("Back")
            self.assertIn("Step 3 of 7: personality", self.text)
            await self.tap("Back")
            self.assertIn("Step 2 of 7: task", self.text)
            self.assertIn("Now: Summarize papers", self.text)
            await self.tap("Keep it")
            self.assertIn("Step 3 of 7", self.text)
            await self.tap("Back")
            await self.tap("Back")
            self.assertIn("Step 1 of 7: name", self.text)
            self.assertIn("Now: Research", self.text)
            await self.type("Researcher")  # typing again replaces the name
            await self.tap("Back")
            await self.tap("Back")
            self.assertIn("control panel", self.text)
            self.assertIn("➕ Create bot", self.labels())

        self.play(scenario)

    def test_menu_cancel_closes_it_and_its_buttons_stop_working(self) -> None:
        async def scenario() -> None:
            await self.open()
            await self.tap("Create bot")
            await self.type("Research")
            await self.tap("Skip")
            await self.tap("Use the base")
            old_button = self.data("Sonnet")
            await self.tap("Cancel")
            self.assertIn("Menu closed", self.text)
            self.assertEqual(self.rows, [])
            self.assertIsNone(self.pool.registry.take_pending(OWNER, ""))  # nothing was requested
            toast = await self.tap("", data=old_button)
            self.assertEqual(toast, "This menu has expired.")
            self.assertIn("Send /bot to open a new one", self.text)
            # With no menu open, text is just text.
            await self.type("hello")
            self.assertIn("I'm the control panel", self.replies[0]["text"])

        self.play(scenario)

    def test_menu_a_restart_or_a_newer_menu_expires_old_buttons(self) -> None:
        async def scenario() -> None:
            await self.walk_to_summary()
            create = self.data("Create")
            self.pool.main.menus.sessions.clear()  # what a restart leaves: no sessions at all
            toast = await self.tap("", data=create)
            self.assertEqual(toast, "This menu has expired.")
            self.assertIn("Consiglieri restarted", self.text)
            self.assertEqual(self.panel_bot.sent, [])  # no half-made request was sent
            await self.open()
            first = self.data("Create bot")
            await self.open()  # a second /bot replaces the first menu
            self.assertEqual(await self.tap("", data=first), "This menu has expired.")

        self.play(scenario)

    def test_menu_only_its_owner_can_press_its_buttons(self) -> None:
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "%d,%d" % (OWNER, STRANGER)

        async def scenario() -> None:
            await self.open()
            await self.tap("Create bot")
            text = self.text
            # An allowed user who is not the one who opened it: the session is not theirs.
            self.assertEqual(await self.tap("", user_id=STRANGER, data=self.data("Cancel")), "This menu has expired.")
            self.sandbox.environ["ALFRED_ALLOWED_USERS"] = str(OWNER)
            await self.open()
            await self.tap("Create bot")
            text = self.text
            self.assertIsNone(await self.tap("", user_id=STRANGER, data=self.data("Cancel")))  # not allowed at all
            self.assertEqual(self.text, text)
            self.assertIn(OWNER, self.pool.main.menus.sessions)

        self.play(scenario)

    def test_menu_bad_input_is_explained_and_asked_again(self) -> None:
        async def scenario() -> None:
            await self.open()
            await self.tap("Create bot")
            await self.type("x" * 80)
            self.assertIn("1 to 64 characters", self.text)
            self.assertIn("Step 1 of 7", self.text)
            await self.type("Research")
            await self.tap("Skip")
            await self.tap("Use the base")
            await self.tap("Type a model name")
            await self.type("not a model!")
            self.assertIn("does not look like a model name", self.text)
            await self.type("claude-opus-5-5")
            self.assertIn("Step 5 of 7", self.text)
            await self.tap("Back")
            self.assertIn("Step 4 of 7", self.text)

        self.play(scenario)

    def test_menu_codex_offers_its_own_models_and_efforts(self) -> None:
        async def scenario() -> None:
            await self.open()
            await self.tap("Create bot")
            await self.type("Coder")
            await self.tap("Skip")
            await self.tap("Use the base")
            await self.tap("Codex")
            self.assertIn("● Codex", self.labels())
            self.assertNotIn("Sonnet", " ".join(self.labels()))
            await self.tap("Next")
            self.assertNotIn("xhigh", self.labels())
            self.assertIn("high", self.labels())

        self.play(scenario)

    def test_menu_dangerous_switches_warn(self) -> None:
        async def scenario() -> None:
            await self.open()
            await self.tap("Create bot")
            await self.type("Risky")
            await self.tap("Skip")
            await self.tap("Use the base")
            await self.tap("Next")
            await self.tap("Next")
            await self.tap("Customize")
            await self.tap("Shell")
            self.assertIn("⚠️ Full shell: any command, inside Claude Code's sandbox", self.text)
            await self.tap("Sandbox")
            self.assertIn("without the sandbox", self.text)
            for _ in range(3):
                await self.tap("Mode")
            self.assertIn("⚠️ Mode: bypassPermissions", self.labels())
            self.assertIn("bypassPermissions: no checks", self.text)
            await self.tap("Back")
            await self.tap("Basic")
            self.assertIn("Permissions: 🔒 Basic", self.text)
            await self.tap("Back")
            await self.tap("Customize")  # now from Basic
            self.assertIn("🔐 Mode: dontAsk", self.labels())
            self.assertIn("🐚 Shell: only alfred", self.labels())

        self.play(scenario)

    def test_menu_every_button_fits_telegrams_64_bytes(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            seen: List[str] = []
            await self.open()
            seen += [data for row in self.rows for _, data in row]
            for path in (["Create bot"], ["My bots"], ["My bots", "Research"], ["Help"]):
                await self.open()
                for label in path:
                    await self.tap(label)
                seen += [data for row in self.rows for _, data in row]
            await self.walk_to_summary()
            seen += [data for row in self.rows for _, data in row]
            self.assertTrue(seen)
            for data in seen:
                self.assertLessEqual(len(data.encode("utf-8")), 64, data)
            with self.assertRaises(ValueError):
                menus.callback("x" * 60, "destroy!")

        self.play(scenario)

    # -- existing bots --

    def test_menu_my_bots_edits_model_effort_task_permissions_and_pauses(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            self.assertIn("Research (@alfredpanel_research_bot), on", self.text)
            await self.tap("Model")
            await self.tap("Opus")
            self.assertIn("Model set to opus", self.text)
            self.assertEqual(self.pool.registry.get(RESEARCH).settings["model"], "opus")
            await self.tap("Effort")
            await self.tap("max")
            self.assertEqual(self.pool.registry.get(RESEARCH).settings["effort"], "max")
            await self.tap("Task")
            await self.type("Read arXiv every morning")
            self.assertIn("Task changed", self.text)
            self.assertEqual(self.pool.registry.get(RESEARCH).settings["task"], "Read arXiv every morning")
            await self.tap("Permissions")
            await self.tap("Customize")
            await self.tap("Web")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("web"), "on")
            self.assertIn("✅ 🌐 Web search and pages", self.labels())
            await self.tap("Back")
            await self.tap("Back")
            await self.tap("Pause")
            self.assertFalse(self.pool.registry.get(RESEARCH).enabled)
            self.assertNotIn(RESEARCH, self.pool.frontends)
            await self.tap("Resume")
            self.assertIn(RESEARCH, self.pool.frontends)

        self.play(scenario)

    # -- security presets --

    async def walk_to_permissions(self, name: str = "Research") -> None:
        await self.open()
        await self.tap("Create bot")
        await self.type(name)
        await self.tap("Skip")
        await self.tap("Use the base")
        await self.tap("Next")
        await self.tap("Next")
        self.assertIn("Step 6 of 7: permissions", self.text)

    def test_menu_presets_are_offered_with_what_each_can_do(self) -> None:
        async def scenario() -> None:
            await self.walk_to_permissions()
            self.assertEqual(self.labels(), ["🔒 Basic", "● ⭐ Assistant (recommended)", "🛠 Power", "🔓 Full access",
                                             "🧑‍💼 Bot admin", "🎛 Customize", "⬅️ Back", "✖️ Cancel"])
            for key in presets.choices():
                self.assertIn(presets.card(key), self.text)
            self.assertIn("Now: ⭐ Assistant", self.text)
            self.assertNotIn("Web search and pages", " ".join(self.labels()))  # no switches here

            await self.tap("Power")  # choosing one moves on
            self.assertIn("Step 7 of 7", self.text)
            self.assertIn("Permissions: 🛠 Power", self.text)
            self.assertIn("⚠️ Browser", self.text)
            await self.tap("Create")
            self.panel_bot.tokens[RESEARCH] = "%d:%s" % (RESEARCH, "A" * 35)
            record = await self.pool.on_managed_bot({"id": OWNER}, {"id": RESEARCH, "username": "alfredpanel_research_bot"})
            self.assertEqual(record.permissions, presets.apply("power", {}))

        self.play(scenario)

    def test_menu_customize_starts_from_the_preset_and_back_returns_to_the_presets(self) -> None:
        async def scenario() -> None:
            await self.walk_to_permissions()
            await self.tap("Basic")
            self.assertIn("Permissions: 🔒 Basic", self.text)
            await self.tap("Back")
            self.assertIn("● 🔒 Basic", self.labels())
            await self.tap("Customize")
            self.assertIn("Starting point: 🔒 Basic", self.text)
            self.assertIn("⬜ 🌐 Web search and pages", self.labels())
            await self.tap("Web")
            self.assertIn("Now: 🎛 Custom (from 🔒 Basic: + web)", self.text)
            await self.tap("Back")
            self.assertIn("Step 6 of 7: permissions", self.text)
            self.assertIn("Now: 🎛 Custom (from 🔒 Basic: + web)", self.text)
            self.assertNotIn("● ", " ".join(self.labels()))  # no preset is current
            await self.tap("Customize")
            self.assertIn("✅ 🌐 Web search and pages", self.labels())  # the change was kept
            await self.tap("Next")
            self.assertIn("Permissions: 🎛 Custom (from 🔒 Basic: + web)", self.text)
            await self.tap("Back")
            await self.tap("Assistant")  # a preset replaces the custom set
            self.assertIn("Permissions: ⭐ Assistant", self.text)

        self.play(scenario)

    def test_menu_an_existing_bot_shows_its_preset_and_asks_before_granting_more(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            self.assertIn("Permissions: 🔒 Basic", self.text)
            await self.tap("Permissions")
            self.assertIn("Now: 🔒 Basic", self.text)
            self.assertIn("● 🔒 Basic", self.labels())
            self.assertIn("🎛 Customize", self.labels())

            await self.tap("Power")
            self.assertIn("Give @alfredpanel_research_bot more permissions?", self.text)
            self.assertIn("It gains: + file edits, + web, + browser, mode: acceptEdits, shell: any command", self.text)
            await self.tap("No, back")
            self.assertEqual(self.pool.registry.get(RESEARCH).permissions, {})
            self.assertIn("Now: 🔒 Basic", self.text)

            await self.tap("Power")
            await self.tap("Yes, switch to Power")
            self.assertEqual(self.pool.registry.get(RESEARCH).permissions, presets.apply("power", {}))
            self.assertIn("now has 🛠 Power", self.text)
            self.assertIn("● 🛠 Power", self.labels())

            await self.tap("Assistant")  # fewer permissions: no question
            self.assertIn("now has ⭐ Assistant", self.text)
            self.assertEqual(self.pool.registry.get(RESEARCH).permissions, {"web": "on", "edit": "on"})
            await self.tap("Assistant")
            self.assertIn("already has ⭐ Assistant", self.text)

            await self.tap("Customize")
            await self.tap("Browser")
            self.assertIn("Now: 🎛 Custom (from ⭐ Assistant: + browser)", self.text)
            await self.tap("Back")
            self.assertIn("Now: 🎛 Custom (from ⭐ Assistant: + browser)", self.text)
            await self.tap("Back")
            self.assertIn("Permissions: 🎛 Custom (from ⭐ Assistant: + browser)", self.text)

        self.play(scenario)

    def test_menu_bots_made_before_presets_keep_their_permissions(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            record = self.pool.registry.get(RESEARCH)
            # What the old Broad button stored, plus an extra folder of its own.
            old = {"web": "on", "edit": "on", "browser": "on", "shell": "full", "mode": "acceptEdits", "folders": str(self.work)}
            record.permissions = dict(old)
            self.pool.registry.save(record)
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            self.assertIn("Permissions: 🛠 Power", self.text)
            await self.tap("Permissions")
            self.assertIn("● 🛠 Power", self.labels())
            self.assertEqual(self.pool.registry.get(RESEARCH).permissions, old)  # reading it changed nothing

            await self.tap("Basic")  # a preset keeps the bot's own folders
            self.assertEqual(self.pool.registry.get(RESEARCH).permissions, {"folders": str(self.work)})

            record = self.pool.registry.get(RESEARCH)
            record.permissions = {"contact": "on", "mode": "auto"}  # matches no preset
            self.pool.registry.save(record)
            await self.tap("Back")
            self.assertIn("Permissions: 🎛 Custom (from 🔒 Basic: + other chats, mode: auto)", self.text)

        self.play(scenario)

    def test_menu_the_shared_memory_lists_adds_shows_and_removes(self) -> None:
        async def scenario() -> None:
            from alfred import shared_memory

            await self.open()
            await self.tap("Shared memory")
            self.assertIn("never instructions", self.text)
            self.assertIn("It is empty.", self.text)
            await self.tap("Add a fact")
            await self.type("no colon here")
            self.assertIn("write it as topic: fact", self.text)
            await self.type('keys: password = "hunter2hunter2"')
            self.assertIn("looks like a secret", self.text)
            await self.type("user: lives in Madrid")
            self.assertIn("Added.", self.text)
            self.assertIn("1. lives in Madrid", self.text)
            self.assertEqual(shared_memory.facts(self.config, "user"), ["lives in Madrid"])
            await self.tap("Back")
            self.assertIn("📄 user", self.labels())
            await self.tap("user")
            await self.tap("Remove topic")
            self.assertIn("Remove the shared topic user", self.text)
            await self.tap("No, back")
            await self.tap("Remove topic")
            await self.tap("Yes, remove it")
            self.assertIn("Removed user", self.text)
            self.assertEqual(shared_memory.entries(self.config), [])

        self.play(scenario)

    def test_menu_destroy_asks_first(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            await self.tap("Destroy")
            self.assertIn("Destroy Research", self.text)
            await self.tap("No, back")
            self.assertIn(RESEARCH, self.pool.frontends)
            await self.tap("Destroy")
            await self.tap("Yes, destroy it")
            self.assertIsNone(self.pool.registry.get(RESEARCH))
            self.assertNotIn(RESEARCH, self.pool.frontends)
            self.assertIn("Destroyed @alfredpanel_research_bot", self.text)
            self.assertIn("No bots yet.", self.text)

        self.play(scenario)

    # -- cross-agent access --

    def test_menu_cross_agent_access_needs_a_warning_is_audited_and_stands_out(self) -> None:
        async def scenario() -> None:
            from alfred import bots

            # Never offered at creation, whatever the preset.
            await self.walk_to_summary()
            await self.tap("Back")
            self.assertIn("granted only to a bot that exists", self.text)
            for preset in ("Basic", "Assistant", "Power", "Full access"):
                self.assertFalse([label for label in self.labels() if "ross-agent" in label], preset)
                await self.tap(preset)
                self.assertNotIn("ross-agent access", self.text.split("Permissions:")[1].split("\n")[0], preset)
                await self.tap("Back")
            await self.tap("Customize")
            self.assertFalse([label for label in self.labels() if "ross-agent" in label])

            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            await self.tap("Permissions")
            self.assertIn("🔒 Cross-agent access: off (above full access)", self.labels())
            self.assertIn("Other agents' memory and tools: blocked", self.text)
            self.assertIn("⚠️ Cross-agent access\nCan: on top of its preset", self.text)  # its card, after the presets
            await self.tap("Customize")
            self.assertIn("🔒 Cross-agent access: off (above full access)", self.labels())
            self.assertIn("cannot read the memory of other agents", self.text)
            await self.tap("Back")
            await self.tap("Cross-agent access")
            self.assertIn("⚠️ Cross-agent access for Research", self.text)
            self.assertIn("audit log", self.text)
            await self.tap("No, back")
            self.assertIn("Permissions of", self.text)
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            self.assertEqual(bots.read_audit(self.config), [])

            # The presets and switches never touch it, before or after it is granted.
            await self.tap("Power")
            await self.tap("Yes, switch to Power")
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            await self.tap("Cross-agent access")
            await self.tap("Yes, grant cross-agent access")
            self.assertTrue(self.pool.registry.get(RESEARCH).cross_agent)
            self.assertIn("Cross-agent access granted", self.text)
            self.assertIn("⚠️ CROSS-AGENT ACCESS ON · withdraw", self.labels())
            self.assertIn("Now: 🛠 Power + ⚠️ Cross-agent access", self.text)
            await self.tap("Customize")
            self.assertIn("⚠️ CROSS-AGENT ACCESS: it reads the memory", self.text)
            await self.tap("Shell")
            self.assertTrue(self.pool.registry.get(RESEARCH).cross_agent)
            await self.tap("Back")
            await self.tap("Basic")  # fewer permissions: applied at once, and cross-agent access stays
            self.assertEqual(self.pool.registry.get(RESEARCH).permissions, {})
            self.assertTrue(self.pool.registry.get(RESEARCH).cross_agent)
            self.assertIn("Now: 🔒 Basic + ⚠️ Cross-agent access", self.text)

            await self.tap("Back")
            self.assertIn("CROSS-AGENT ACCESS ON since", self.text)  # the bot's own screen
            self.assertIn("Permissions: 🔒 Basic + ⚠️ Cross-agent access", self.text)
            await self.tap("Back")
            self.assertIn("Research @alfredpanel_research_bot ⚠️ CROSS-AGENT", self.labels())  # the list
            await self.tap("Research")
            await self.tap("Permissions")
            await self.tap("withdraw")
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            self.assertIn("withdrawn", self.text)
            entries = bots.read_audit(self.config)
            self.assertEqual([(entry["event"], entry["via"], entry["by"], entry["bot_id"]) for entry in entries],
                             [("cross_agent_granted", "menu", OWNER, RESEARCH), ("cross_agent_revoked", "menu", OWNER, RESEARCH)])

        self.play(scenario)

    def test_menu_cross_agent_buttons_fit_and_a_stranger_cannot_grant(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.open()
            for label in ("My bots", "Research", "Permissions", "Cross-agent access"):
                await self.tap(label)
            for row in self.rows:
                for _, data in row:
                    self.assertLessEqual(len(data.encode("utf-8")), 64, data)
            grant = self.data("Yes, grant cross-agent access")
            await self.tap("", user_id=STRANGER, data=grant)
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            # Granting goes through the warning screen: the action from another screen only shows it.
            await self.tap("No, back")
            await self.tap("", data=grant)
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            self.assertIn("⚠️ Cross-agent access for Research", self.text)

        self.play(scenario)


    # -- everything the text commands manage is also in the menu --

    def test_menu_a_bots_reminders_are_listed_and_cancelled(self) -> None:
        from datetime import timedelta

        from alfred import scheduler

        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            soon = scheduler.utc_now() + timedelta(hours=2)
            first = self.pool.store.add(chat_id=OWNER, due=soon, text="Call Sam", bot_id=RESEARCH)
            self.pool.store.add(chat_id=555, due=soon + timedelta(hours=1), text="Weekly digest",
                                kind=scheduler.KIND_TASK, repeat_seconds=7 * 86400, bot_id=RESEARCH)
            self.pool.store.add(chat_id=OWNER, due=soon, text="Someone else's", bot_id=CHEF_ID)
            await self.open()
            for label in ("My bots", "Research", "Reminders"):
                await self.tap(label)
            self.assertIn("1. ", self.text)
            self.assertIn("message · Call Sam", self.text)
            self.assertIn("task (every 7d) · Weekly digest", self.text)  # in any of its chats
            self.assertNotIn("Someone else's", self.text)
            await self.tap("🗑 1")
            self.assertIn("Cancelled: Call Sam", self.text)
            self.assertEqual(self.pool.store.get(first["id"])["status"], scheduler.STATUS_CANCELLED)
            self.assertNotIn("Call Sam\n", self.text.split("\n\n", 1)[1])

        self.play(scenario)

    def test_menu_the_old_main_bots_reminders_are_kept_listed_and_can_be_cancelled(self) -> None:
        from datetime import timedelta

        from alfred import scheduler

        async def scenario() -> None:
            soon = scheduler.utc_now() + timedelta(hours=2)
            kept = self.pool.store.add(chat_id=OWNER, due=soon, text="Old reminder", bot_id=0)
            other = self.pool.store.add(chat_id=STRANGER, due=soon, text="Another chat's", bot_id=0)
            await self.open()
            await self.tap("Old reminders (1)")
            self.assertIn("Old reminder", self.text)
            self.assertNotIn("Another chat's", self.text)
            self.assertIn("still posted here", self.text)
            await self.tap("🗑 1")
            self.assertEqual(self.pool.store.get(kept["id"])["status"], scheduler.STATUS_CANCELLED)
            self.assertEqual(self.pool.store.get(other["id"])["status"], scheduler.STATUS_SCHEDULED)  # never deleted
            await self.tap("Back")
            self.assertNotIn("Old reminders", " ".join(self.labels()))

        self.play(scenario)

    def test_menu_usage_alerts_are_listed_and_removed(self) -> None:
        from alfred import usage_alerts

        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research -usage")
            store = usage_alerts.AlertStore.for_bot(self.config, RESEARCH)
            store.add("session.used > 80", note="slow down", chat_id=OWNER)
            await self.open()
            for label in ("My bots", "Research", "Usage alerts"):
                await self.tap(label)
            self.assertIn("1. when session.used > 80", self.text)
            self.assertIn("Plan usage permission is off", self.text)
            self.assertIn("usage monitor is off", self.text)
            await self.tap("🗑 1")
            self.assertIn("Removed the alert session.used > 80", self.text)
            self.assertEqual(store.alerts(), [])

        self.play(scenario)

    def test_menu_folders_and_rules_are_typed_in_and_checked(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.open()
            for label in ("My bots", "Research", "Folders and rules", "Working folder"):
                await self.tap(label)
            self.assertIn("Send the new working folder", self.text)
            await self.type(str(self.work))
            self.assertEqual(self.pool.registry.get(RESEARCH).settings["workdir"], str(self.work))
            self.assertIn("Working folder: %s" % self.work, self.text)
            await self.tap("Deny rules")
            await self.type("Bash(rm *)")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("deny"), "Bash(rm *)")
            await self.tap("Extra folders")
            await self.type(str(self.config.home))  # Consiglieri's own state: refused
            self.assertIn("⚠️", self.text)
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("folders"), "")
            await self.type("default")
            await self.tap("Deny rules")
            await self.type("default")
            self.assertEqual(self.pool.registry.get(RESEARCH).permission("deny"), "")

        self.play(scenario)

    def test_menu_the_audit_and_removing_one_shared_fact(self) -> None:
        from alfred import shared_memory

        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.pool.set_cross_agent(self.pool.registry.get(RESEARCH), True, OWNER, "command")
            await self.open()
            await self.tap("Audit")
            self.assertIn("granted @alfredpanel_research_bot by %d (command)" % OWNER, self.text)
            await self.tap("Back")
            shared_memory.add(self.config, "user", "Lives in Madrid")
            shared_memory.add(self.config, "user", "Works as a designer")
            await self.tap("Shared memory")
            await self.tap("📄 user")
            await self.tap("🗑 1")
            self.assertIn("Removed this fact", self.text)
            self.assertEqual(shared_memory.facts(self.config, "user"), ["Works as a designer"])

        self.play(scenario)

    # -- per-bot browser and credentials --

    def test_menu_browser_choice_stores_clears_and_shows_on_screen(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research +browser")
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            self.assertIn("Browser: default (Orca)", self.text)
            self.assertIn("🧭 Browser: default (Orca)", self.labels())

            await self.tap("Browser")
            self.assertIn("Default uses Consiglieri's setting in .env (now: Orca)", self.text)
            self.assertEqual(self.labels(), ["● Default (Orca)", "Orca", "Chrome", "Own browser", "⬅️ Back"])
            for row in self.rows:
                for _, data in row:
                    self.assertLessEqual(len(data.encode("utf-8")), 64, data)

            await self.tap("Own browser")
            self.assertIn("Browser set to Own browser", self.text)
            self.assertIn("Browser: Own browser", self.text)
            self.assertIn("🧭 Browser: Own browser", self.labels())
            self.assertEqual(self.pool.registry.get(RESEARCH).settings["browser"], "bot")
            self.assertEqual(self.pool.registry.config_for(RESEARCH).browser, "bot")

            await self.tap("Browser")
            self.assertIn("● Own browser", self.labels())
            await self.tap("Chrome")
            self.assertEqual(self.pool.registry.get(RESEARCH).settings["browser"], "chrome")
            self.assertIn("Browser: Chrome", self.text)
            self.assertIn("🧭 Browser: Chrome", self.labels())

            # Default follows the panel config's .env value and clears the per-bot override.
            self.sandbox.environ["ALFRED_BROWSER"] = "chrome"
            await self.tap("Browser")
            self.assertIn("Default (Chrome)", self.labels())
            self.assertIn("now: Chrome", self.text)
            await self.tap("Default")
            self.assertNotIn("browser", self.pool.registry.get(RESEARCH).settings)
            self.assertIn("Browser set to the default (Chrome)", self.text)
            self.assertIn("Browser: default (Chrome)", self.text)
            self.assertIn("🧭 Browser: default (Chrome)", self.labels())

            # With browser permission off, the choice screen says so.
            await self.pool.set_permission(self.pool.registry.get(RESEARCH), "browser", "off")
            self.sandbox.environ["ALFRED_BROWSER"] = "bot"
            await self.tap("Browser")
            self.assertIn("● Default (Own browser)", self.labels())
            self.assertIn("Browser permission is off", self.text)

        self.play(scenario)

    def test_menu_credentials_status_errors_thread_and_confirmed_changes(self) -> None:
        async def scenario() -> None:
            # Here nobody may grant from Telegram (several panel users and none listed): the grant path is shut.
            self.sandbox.environ["ALFRED_CREDENTIALS_GRANTERS"] = "none"
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research")
            await self.open()
            await self.tap("My bots")
            await self.tap("Research")
            self.assertIn("🔑 Credentials", self.labels())

            # Store not set up, neither creds nor own browser enabled.
            await self.tap("Credentials")
            self.assertIn("alfred creds setup", self.text)
            self.assertIn("Creds permission: off", self.text)
            self.assertIn("Without both the creds permission and its own browser", self.text)
            self.assertEqual(self.labels(), ["⬅️ Back"])

            # Turn on creds + browser + Own browser: readiness warning clears.
            await self.pool.set_permission(self.pool.registry.get(RESEARCH), "creds", "on")
            await self.pool.set_permission(self.pool.registry.get(RESEARCH), "browser", "on")
            await self.pool.set_setting(self.pool.registry.get(RESEARCH), "browser", "bot")
            await self.tap("Back")
            await self.tap("Credentials")
            self.assertIn("Creds permission: on", self.text)
            self.assertIn("Browser: Own browser", self.text)
            self.assertNotIn("Without both", self.text)

            # CredentialError becomes a friendly message instead of failing the handler.
            with mock.patch.object(keepass.KeePass, "is_set_up", return_value=True), \
                    mock.patch.object(keepass.KeePass, "granted_to",
                                      side_effect=keepass.CredentialError("keepassxc-cli is not installed")), \
                    mock.patch.object(keepass.KeePass, "secrets_for", side_effect=AssertionError("must not read secrets")):
                await self.tap("Back")
                await self.tap("Credentials")
            self.assertIn("⚠️ Could not read credentials: keepassxc-cli is not installed", self.text)
            self.assertEqual(self.labels(), ["⬅️ Back"])

            # Runs KeePass off the event loop (asyncio.to_thread), handles revoke CredentialError, and withdraws
            # with confirmation, audit and chat notice even without keepassxc-cli installed.
            tum = keepass.Credential(name="tum", username="ge12abc",
                                     urls=["https://moodle.tum.de", "https://login.tum.de"], totp=True, bots=[RESEARCH])
            granted: List[keepass.Credential] = [tum]

            def fake_revoke(store_self: Any, name: str, bot_id: int) -> bool:
                self.assertEqual((name, bot_id), ("tum", RESEARCH))
                granted.clear()
                return True

            with mock.patch.object(keepass.KeePass, "is_set_up", return_value=True), \
                    mock.patch.object(keepass.KeePass, "granted_to", side_effect=lambda bot_id: list(granted)), \
                    mock.patch.object(keepass.KeePass, "secrets_for", side_effect=AssertionError("must not read secrets")), \
                    mock.patch.object(keepass.KeePass, "grant", side_effect=AssertionError("no grant path from Telegram")), \
                    mock.patch.object(menus.asyncio, "to_thread", wraps=asyncio.to_thread) as to_thread:
                await self.tap("Back")
                await self.tap("Credentials")
                self.assertTrue(to_thread.called)
                self.assertIn("tum", self.text)
                self.assertIn("ge12abc", self.text)
                self.assertIn("https://moodle.tum.de, https://login.tum.de", self.text)
                self.assertIn("TOTP: yes", self.text)
                self.assertIn("🔒 Withdraw tum", self.labels())
                self.assertFalse(any("Grant" in label for label in self.labels()))
                self.assertIn("Only the users in ALFRED_CREDENTIALS_GRANTERS", self.text)

                # Forged actions cannot grant or skip confirmation.
                token = self.pool.main.menus.sessions[OWNER].token
                for forged in ("credgrant:tum", "grant:tum", "creds:grant", "credrevoke!"):
                    await self.tap("", data=menus.callback(token, forged))
                self.assertEqual(len(granted), 1)

                # Revoke error is turned into a friendly notice with no audit or chat notice.
                with mock.patch.object(keepass.KeePass, "revoke",
                                       side_effect=keepass.CredentialError("database busy")):
                    await self.tap("Withdraw tum")
                    self.assertIn("Withdraw credential «tum»", self.text)
                    await self.tap("Yes, withdraw tum")
                self.assertIn("⚠️ Could not withdraw tum: database busy", self.text)
                self.assertEqual(bots.read_audit(self.config), [])
                self.assertEqual(self.pool.store.pending_for_bot(RESEARCH), [])

                # Successful withdraw revokes, writes audit (by=OWNER, via="panel") and notifies the bot's chat.
                with mock.patch.object(keepass.KeePass, "revoke", fake_revoke):
                    await self.tap("Withdraw tum")
                    await self.tap("No, back")
                    self.assertEqual(len(granted), 1)
                    await self.tap("Withdraw tum")
                    calls_before = to_thread.call_count
                    await self.tap("Yes, withdraw tum")
                    self.assertGreater(to_thread.call_count, calls_before)

            self.assertIn("Withdrew «tum» from @alfredpanel_research_bot", self.text)
            self.assertIn("No credentials are granted", self.text)
            entries = bots.read_audit(self.config)
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0]["event"], entries[0]["by"], entries[0]["via"],
                              entries[0]["credential"], entries[0]["bot_id"]),
                             ("credential_revoked", OWNER, "panel", "tum", RESEARCH))
            item = self.pool.store.claim_due()
            self.assertIsNotNone(item)
            self.assertEqual((item["bot_id"], item["chat_id"]), (RESEARCH, OWNER))
            self.assertIn("You withdrew the credential «tum» from me.", item["message"])
            await self.pool.deliver(item)
            self.assertTrue(any("You withdrew the credential «tum» from me." in t
                                for t in self.pool.frontends[RESEARCH].bot.texts))

        self.play(scenario)

    @unittest.skipUnless(HAS_CLI, "keepassxc-cli is not installed")
    def test_menu_credentials_with_real_keepass_lists_only_bots_own_never_secrets_and_withdraws(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "alfredpanel_research_bot", "Research", "/bot new Research +browser +creds")
            await self.create(CHEF_ID, "alfredpanel_chef_bot", "Chef", "/bot new Chef +browser +creds")
            await self.pool.set_setting(self.pool.registry.get(RESEARCH), "browser", "bot")

            store = keepass.KeePass(self.config)
            store.setup()
            secret_pw = 'p&ss<w"rd-only-for-keepass'
            other_pw = "chef-mail-secret-pw"
            store.add("tum", ["moodle.tum.de", "login.tum.de"], "ge12abc", secret_pw, TOTP_SEED)
            store.add("mail", ["mail.example.org"], "chef@example.org", other_pw, "")
            store.grant("tum", RESEARCH)
            store.grant("mail", CHEF_ID)
            current_totp = keepass.totp_code(keepass.check_totp(TOTP_SEED))

            with mock.patch.object(keepass.KeePass, "secrets_for",
                                   side_effect=AssertionError("panel must never read secrets")):
                await self.open()
                for label in ("My bots", "Research", "Credentials"):
                    await self.tap(label)
                # Lists only Research's credentials, with metadata, never any secret.
                self.assertIn("tum", self.text)
                self.assertIn("ge12abc", self.text)
                self.assertIn("https://moodle.tum.de, https://login.tum.de", self.text)
                self.assertIn("TOTP: yes", self.text)
                self.assertIn("🔒 Withdraw tum", self.labels())
                granted_part, offered_part = self.text.split("Others you can grant it:")
                self.assertNotIn("mail", granted_part)
                self.assertIn("• mail — sites: https://mail.example.org", offered_part)
                self.assertIn("🔓 Grant mail", self.labels())
                self.assertNotIn("chef@example.org", self.text)
                for secret in (secret_pw, other_pw, TOTP_SEED, current_totp):
                    self.assertNotIn(secret, self.text)
                for row in self.rows:
                    for _, data in row:
                        self.assertLessEqual(len(data.encode("utf-8")), 64, data)

                # Chef sees only mail (no TOTP), never tum.
                for label in ("Back", "Back", "Chef", "Credentials"):
                    await self.tap(label)
                self.assertIn("mail", self.text)
                self.assertIn("chef@example.org", self.text)
                self.assertIn("https://mail.example.org", self.text)
                self.assertIn("TOTP: no", self.text)
                self.assertIn("🔒 Withdraw mail", self.labels())
                self.assertNotIn("ge12abc", self.text)
                self.assertIn("🔓 Grant tum", self.labels())
                for secret in (secret_pw, other_pw, TOTP_SEED, current_totp):
                    self.assertNotIn(secret, self.text)

                # Withdraw tum from Research: asks first, then revokes in KeePassXC, audits and notifies.
                for label in ("Back", "Back", "Research", "Credentials", "Withdraw tum"):
                    await self.tap(label)
                self.assertIn("Withdraw credential «tum» from @alfredpanel_research_bot?", self.text)
                for row in self.rows:
                    for _, data in row:
                        self.assertLessEqual(len(data.encode("utf-8")), 64, data)
                self.assertEqual([c.name for c in store.granted_to(RESEARCH)], ["tum"])
                self.assertEqual(bots.read_audit(self.config), [])

                await self.tap("No, back")
                self.assertEqual([c.name for c in store.granted_to(RESEARCH)], ["tum"])

                await self.tap("Withdraw tum")
                await self.tap("Yes, withdraw tum")
                self.assertEqual(store.granted_to(RESEARCH), [])
                self.assertEqual([c.name for c in store.granted_to(CHEF_ID)], ["mail"])
                self.assertIn("Withdrew «tum»", self.text)
                self.assertIn("No credentials are granted", self.text)
                self.assertEqual(sorted(self.labels()), sorted(["🔓 Grant mail", "🔓 Grant tum", "⬅️ Back"]))

                # Grant tum back from the panel: asks first, then grants in KeePassXC, audits and notifies.
                await self.tap("Grant tum")
                self.assertIn("Grant credential «tum» to @alfredpanel_research_bot?", self.text)
                self.assertIn("User: ge12abc", self.text)
                for secret in (secret_pw, other_pw, TOTP_SEED, current_totp):
                    self.assertNotIn(secret, self.text)
                await self.tap("No, back")
                self.assertEqual(store.granted_to(RESEARCH), [])
                await self.tap("Grant tum")
                await self.tap("Yes, grant tum")
                self.assertEqual([c.name for c in store.granted_to(RESEARCH)], ["tum"])
                self.assertEqual([c.name for c in store.granted_to(CHEF_ID)], ["mail"])
                self.assertIn("Granted «tum» to @alfredpanel_research_bot", self.text)
                self.assertIn("🔒 Withdraw tum", self.labels())

                # Audit lines match creds_cli (by=OWNER, via="panel") and show in the Audit screen.
                entries = bots.read_audit(self.config)
                self.assertEqual([(e["event"], e["by"], e["via"], e["credential"], e["bot_id"]) for e in entries],
                                 [("credential_revoked", OWNER, "panel", "tum", RESEARCH),
                                  ("credential_granted", OWNER, "panel", "tum", RESEARCH)])
                await self.open()
                await self.tap("Audit")
                self.assertIn("credential withdrawn from @alfredpanel_research_bot by %d (panel) [credential tum]" % OWNER,
                              self.text)

                # Bot chat notification is queued and delivered only to Research's chat.
                while True:
                    item = self.pool.store.claim_due()
                    if item is None:
                        break
                    await self.pool.deliver(item)
                self.assertTrue(any("You withdrew the credential «tum» from me." in t
                                    for t in self.pool.frontends[RESEARCH].bot.texts))
                self.assertTrue(any("You granted me the credential «tum»" in t
                                    for t in self.pool.frontends[RESEARCH].bot.texts))
                self.assertEqual(self.pool.frontends[CHEF_ID].bot.texts, [])

        self.play(scenario)


if __name__ == "__main__":
    unittest.main()
