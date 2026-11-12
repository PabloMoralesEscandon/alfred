"""The /bot menus, driven like a user would: open /bot, tap buttons by their label, type answers."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

try:
    from test_panel import OWNER, RESEARCH, STRANGER, PanelHarness, update_for
except ImportError:  # pragma: no cover
    PanelHarness = None  # type: ignore

from alfred import menus, presets


@unittest.skipIf(PanelHarness is None, "python-telegram-bot is not installed")
class MenuTests(PanelHarness):  # type: ignore[misc,valid-type]
    # -- driving the menu --

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
        await self.tap("Sonnet")
        await self.tap("high")
        await self.tap("Customize")  # starts from the recommended preset, Assistant
        await self.tap("Browser")
        await self.tap("Next")

    # -- the assistant --

    def test_menu_the_assistant_walks_through_every_step_and_creates_the_request(self) -> None:
        async def scenario() -> None:
            await self.open()
            self.assertEqual([label for label in self.labels()], ["➕ Create bot", "🤖 My bots", "❓ Help", "✖️ Close"])
            await self.tap("Create bot")
            self.assertIn("Step 1 of 6: name", self.text)
            await self.type("Gestor de Calendarios")
            self.assertIn("Step 2 of 6: task", self.text)
            await self.type("Keep my calendar tidy and remind me of events.")
            self.assertIn("Step 3 of 6: model", self.text)
            await self.tap("Sonnet")
            self.assertIn("Step 4 of 6: effort", self.text)
            await self.tap("high")
            self.assertIn("Step 5 of 6: permissions", self.text)
            self.assertIn("● ⭐ Assistant (recommended)", self.labels())
            await self.tap("Customize")
            self.assertIn("✅ 🌐 Web search and pages", self.labels())
            self.assertIn("✅ 📁 Edit its working folder", self.labels())
            await self.tap("Browser")
            self.assertIn("⚠️ Browser", self.text)  # the risky choice is flagged
            await self.tap("Next")
            self.assertIn("Step 6 of 6", self.text)
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
            await self.tap("Opus")
            await self.tap("Back")
            self.assertIn("Step 3 of 6: model", self.text)
            self.assertIn("● Opus", self.labels())
            await self.tap("Back")
            self.assertIn("Step 2 of 6: task", self.text)
            self.assertIn("Now: Summarize papers", self.text)
            await self.tap("Keep it")
            self.assertIn("Step 3 of 6", self.text)
            await self.tap("Back")
            await self.tap("Back")
            self.assertIn("Step 1 of 6: name", self.text)
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
            self.assertIn("Alfred restarted", self.text)
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
            self.assertIn("Step 1 of 6", self.text)
            await self.type("Research")
            await self.tap("Skip")
            await self.tap("Type a model name")
            await self.type("not a model!")
            self.assertIn("does not look like a model name", self.text)
            await self.type("claude-opus-5-5")
            self.assertIn("Step 4 of 6", self.text)
            await self.tap("Back")
            self.assertIn("Step 3 of 6", self.text)

        self.play(scenario)

    def test_menu_codex_offers_its_own_models_and_efforts(self) -> None:
        async def scenario() -> None:
            await self.open()
            await self.tap("Create bot")
            await self.type("Coder")
            await self.tap("Skip")
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
        await self.tap("Next")
        await self.tap("Next")
        self.assertIn("Step 5 of 6: permissions", self.text)

    def test_menu_presets_are_offered_with_what_each_can_do(self) -> None:
        async def scenario() -> None:
            await self.walk_to_permissions()
            self.assertEqual(self.labels(), ["🔒 Basic", "● ⭐ Assistant (recommended)", "🛠 Power", "🔓 Full access",
                                             "🎛 Customize", "⬅️ Back", "✖️ Cancel"])
            for key in presets.keys():
                self.assertIn(presets.card(key), self.text)
            self.assertIn("Now: ⭐ Assistant", self.text)
            self.assertNotIn("Web search and pages", " ".join(self.labels()))  # no switches here

            await self.tap("Power")  # choosing one moves on
            self.assertIn("Step 6 of 6", self.text)
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
            self.assertIn("Step 5 of 6: permissions", self.text)
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


if __name__ == "__main__":
    unittest.main()
