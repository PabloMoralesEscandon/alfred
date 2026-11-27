"""Two allowed users, each with their own bots: neither sees, changes, talks to or reaches into the other's.

The checks hold in the text commands, in the menu (where a button carries only a bot id, which must not be
trusted), in the confirmation buttons, in a bot's own chat, in the `alfred bots` commands a bot runs and in
cross-agent access.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

try:
    from test_panel import OWNER, PanelHarness, token_for, update_for
except ImportError:  # pragma: no cover
    PanelHarness = None  # type: ignore

from alfred import bots, cli, menus

SECOND = 77  # the other allowed user
MINE, THEIRS = 5000000011, 5000000022  # OWNER's bot, SECOND's bot


@unittest.skipIf(PanelHarness is None, "python-telegram-bot is not installed")
class TwoUsersTests(PanelHarness):  # type: ignore[misc,valid-type]
    def setUp(self) -> None:
        super().setUp()
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "%d,%d" % (OWNER, SECOND)

    async def create_for(self, user_id: int, bot_id: int, username: str, name: str) -> bots.BotRecord:
        self.panel_bot.tokens[bot_id] = token_for(bot_id)
        record = await self.pool.on_managed_bot({"id": user_id, "username": "u%d" % user_id},
                                                {"id": bot_id, "username": username, "first_name": name})
        assert record is not None and record.owner_id == user_id
        return record

    async def both(self) -> None:
        await self.create_for(OWNER, MINE, "mine_bot", "Mine")
        await self.create_for(SECOND, THEIRS, "theirs_bot", "Theirs")

    async def command_as(self, user_id: int, text: str) -> List[Dict[str, Any]]:
        replies: List[Dict[str, Any]] = []
        update = update_for(replies, text, user_id, chat_id=user_id)
        name = text.split()[0][1:]
        handler = getattr(self.pool.main, "_cmd_%s" % name, None) or self.pool.main._on_message
        await handler(update, SimpleNamespace(bot=self.panel_bot, args=text.split()[1:]))
        return replies

    async def press(self, user_id: int, data: str) -> Tuple[str, List[str]]:
        """Press a button with exactly this callback data, as this user. Returns the new text and button labels."""
        seen: Dict[str, Any] = {"text": "", "rows": []}

        async def answer(text: Optional[str] = None) -> None:
            return None

        async def edit_message_text(text: str, reply_markup: Any = None) -> None:
            seen["text"] = text
            seen["rows"] = [] if reply_markup is None else [b.text for row in reply_markup.inline_keyboard for b in row]

        update = update_for([], "", user_id, chat_id=user_id)
        update.callback_query = SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text,
                                                message=SimpleNamespace(message_id=1))
        await self.pool.main._on_button(update, SimpleNamespace(bot=self.panel_bot))
        return seen["text"], seen["rows"]

    # -- the text commands ------------------------------------------------------

    def test_each_user_lists_and_finds_only_their_own_bots(self) -> None:
        async def scenario() -> None:
            await self.both()
            mine = (await self.command_as(OWNER, "/bots"))
            theirs = (await self.command_as(SECOND, "/bots"))
            self.assertIn("@mine_bot", self.panel_bot.sent[-2]["text"])
            self.assertNotIn("@theirs_bot", self.panel_bot.sent[-2]["text"])
            self.assertIn("@theirs_bot", self.panel_bot.sent[-1]["text"])
            self.assertNotIn("@mine_bot", self.panel_bot.sent[-1]["text"])
            self.assertEqual((mine, theirs), ([], []))  # both answered through the panel bot, above
            status = await self.command_as(SECOND, "/status")
            self.assertIn("Your bots: 1", status[0]["text"])

        self.play(scenario)

    def test_a_user_cannot_show_change_pause_resume_or_destroy_someone_elses_bot(self) -> None:
        async def scenario() -> None:
            await self.both()
            before = self.pool.registry.get(MINE)
            for text in ("/bot show mine_bot", "/bot set mine_bot task Do something else",
                         "/bot perm mine_bot web on", "/bot perm mine_bot full", "/bot perm mine_bot cross on",
                         "/bot pause mine_bot", "/bot resume mine_bot", "/bot destroy mine_bot",
                         "/bot show %d" % MINE, "/bot show Mine"):
                replies = await self.command_as(SECOND, text)
                self.assertEqual(len(replies), 1, text)
                self.assertIn("Which bot?", replies[0]["text"], text)  # it is not even confirmed to exist
                self.assertIsNone(replies[0]["reply_markup"], text)  # no confirmation button to press
            after = self.pool.registry.get(MINE)
            self.assertEqual(after, before)
            self.assertTrue(self.pool.is_running(MINE))
            self.assertFalse(after.cross_agent)

        self.play(scenario)

    def test_a_confirmation_button_works_only_for_who_asked_and_only_on_their_bot(self) -> None:
        async def scenario() -> None:
            await self.both()
            asked = await self.command_as(OWNER, "/bot destroy mine_bot")
            data = asked[0]["reply_markup"].inline_keyboard[0][0].callback_data
            # The other user presses the owner's button: nothing happens.
            text, _ = await self.press(SECOND, data)
            self.assertIn("expired", text)
            self.assertIsNotNone(self.pool.registry.get(MINE))
            # Even with a nonce of their own, a button can't reach someone else's bot.
            nonce = self.pool.ask_confirmation(MINE, SECOND, "destroy")
            text, _ = await self.press(SECOND, "bot:destroy:%s" % nonce)
            self.assertIn("not yours", text)
            self.assertIsNotNone(self.pool.registry.get(MINE))
            # The owner's own press still works.
            text, _ = await self.press(OWNER, data)
            self.assertIn("Destroying @mine_bot", text)
            self.assertIsNone(self.pool.registry.get(MINE))

        self.play(scenario)

    # -- the menu ------------------------------------------------------------------

    def test_the_menu_shows_and_edits_only_your_own_bots_whatever_a_button_says(self) -> None:
        async def scenario() -> None:
            await self.both()
            opened = await self.command_as(SECOND, "/bot")
            session = self.pool.main.menus.sessions[SECOND]
            token = session.token
            text, labels = await self.press(SECOND, menus.callback(token, "go:list"))
            self.assertTrue(any("@theirs_bot" in label for label in labels), labels)
            self.assertFalse(any("@mine_bot" in label for label in labels), labels)
            # A forged button naming the owner's bot.
            text, labels = await self.press(SECOND, menus.callback(token, "bot:%d" % MINE))
            self.assertIn("not one of yours", text)
            self.assertEqual(session.bot_id, 0)
            # Even if the session somehow points at it, no action reaches it.
            before = self.pool.registry.get(MINE)
            for action in ("pause", "destroy!", "perm:web", "pick:full", "pickok", "crossgrant", "crossrevoke",
                           "edittask", "model:2", "effort:3", "browser:3", "go:bot_browser", "go:bot_creds",
                           "credrevoke:0", "credrevoke!", "shell", "mode"):
                session.bot_id = MINE
                session.screen = ("bot_perms_custom" if action.startswith(("perm", "shell", "mode"))
                                  else "bot_creds_revoke" if action == "credrevoke!" else "bot")
                if action == "credrevoke!":
                    session.cred_name = "tum"
                text, _ = await self.press(SECOND, menus.callback(token, action))
                self.assertNotIn("@mine_bot", text, action)
            self.assertEqual(self.pool.registry.get(MINE), before)
            self.assertTrue(self.pool.is_running(MINE))
            # The other user's own bot is still theirs to change.
            text, _ = await self.press(SECOND, menus.callback(token, "bot:%d" % THEIRS))
            self.assertIn("@theirs_bot", text)
            self.assertTrue(opened)

        self.play(scenario)

    def test_only_the_bots_owner_can_view_or_withdraw_its_credentials_or_change_its_browser(self) -> None:
        from alfred import keepass

        async def scenario() -> None:
            await self.both()
            tum = keepass.Credential(name="tum", username="ge12abc", urls=["https://moodle.tum.de"],
                                     totp=True, bots=[MINE])
            with mock.patch.object(keepass.KeePass, "is_set_up", return_value=True), \
                    mock.patch.object(keepass.KeePass, "granted_to", return_value=[tum]) as listed, \
                    mock.patch.object(keepass.KeePass, "revoke", return_value=True) as revoked:
                await self.command_as(OWNER, "/bot")
                owner_session = self.pool.main.menus.sessions[OWNER]
                await self.press(OWNER, menus.callback(owner_session.token, "bot:%d" % MINE))
                await self.press(OWNER, menus.callback(owner_session.token, "go:bot_creds"))
                await self.press(OWNER, menus.callback(owner_session.token, "credrevoke:0"))
                listed.reset_mock()

                # SECOND presses OWNER's own withdraw-confirmation button: rejected as expired.
                text, _ = await self.press(SECOND, menus.callback(owner_session.token, "credrevoke!"))
                self.assertIn("expired", text)
                revoked.assert_not_called()

                # SECOND opens their own menu and tries to point it at MINE.
                await self.command_as(SECOND, "/bot")
                second_session = self.pool.main.menus.sessions[SECOND]
                second_session.bot_id = MINE
                second_session.creds = [tum]
                second_session.cred_name = "tum"
                second_session.screen = "bot_creds_revoke"
                for action in ("go:bot_creds", "credrevoke:0", "credrevoke!", "browser:3"):
                    second_session.bot_id = MINE
                    second_session.cred_name = "tum"
                    second_session.screen = "bot_creds_revoke" if action == "credrevoke!" else "bot"
                    text, _ = await self.press(SECOND, menus.callback(second_session.token, action))
                    self.assertNotIn("ge12abc", text, action)
                    self.assertNotIn("@mine_bot", text, action)
                listed.assert_not_called()
                revoked.assert_not_called()
                self.assertEqual(bots.read_audit(self.config), [])
                self.assertNotIn("browser", self.pool.registry.get(MINE).settings)

                # OWNER can still confirm withdrawal of their own bot's credential.
                text, _ = await self.press(OWNER, menus.callback(owner_session.token, "credrevoke!"))
                self.assertIn("Withdrew «tum»", text)
                revoked.assert_called_once_with("tum", MINE)

        self.play(scenario)

    def test_with_two_users_only_the_listed_granters_may_grant_credentials_and_only_to_their_own_bots(self) -> None:
        from alfred import keepass

        async def scenario() -> None:
            await self.both()
            tum = keepass.Credential(name="tum", username="ge12abc", urls=["https://moodle.tum.de"], totp=False,
                                     bots=[])
            with mock.patch.object(keepass.KeePass, "is_set_up", return_value=True), \
                    mock.patch.object(keepass.KeePass, "granted_to", return_value=[]), \
                    mock.patch.object(keepass.KeePass, "list", return_value=[tum]), \
                    mock.patch.object(keepass.KeePass, "get", return_value=tum), \
                    mock.patch.object(keepass.KeePass, "grant", return_value=True) as granted:
                # Two panel users and no ALFRED_CREDENTIALS_GRANTERS: nobody gets a grant button, forged or not.
                await self.command_as(SECOND, "/bot")
                second = self.pool.main.menus.sessions[SECOND]
                await self.press(SECOND, menus.callback(second.token, "bot:%d" % THEIRS))
                text, labels = await self.press(SECOND, menus.callback(second.token, "go:bot_creds"))
                self.assertFalse(any("Grant" in label for label in labels), labels)
                second.creds_other = [tum]
                await self.press(SECOND, menus.callback(second.token, "credgrant:0"))
                second.screen, second.cred_name = "bot_creds_grant", "tum"
                await self.press(SECOND, menus.callback(second.token, "credgrant!"))
                granted.assert_not_called()

                # Only OWNER is listed: SECOND still cannot, OWNER can, and only to OWNER's own bot.
                self.sandbox.environ["ALFRED_CREDENTIALS_GRANTERS"] = str(OWNER)
                text, labels = await self.press(SECOND, menus.callback(second.token, "go:bot_creds"))
                self.assertFalse(any("Grant" in label for label in labels), labels)
                second.creds_other = [tum]
                await self.press(SECOND, menus.callback(second.token, "credgrant:0"))
                granted.assert_not_called()

                await self.command_as(OWNER, "/bot")
                owner = self.pool.main.menus.sessions[OWNER]
                owner.bot_id, owner.screen, owner.cred_name, owner.creds_other = THEIRS, "bot_creds_grant", "tum", [tum]
                text, _ = await self.press(OWNER, menus.callback(owner.token, "credgrant!"))
                self.assertNotIn("@theirs_bot", text)
                granted.assert_not_called()

                await self.press(OWNER, menus.callback(owner.token, "bot:%d" % MINE))
                text, labels = await self.press(OWNER, menus.callback(owner.token, "go:bot_creds"))
                self.assertIn("🔓 Grant tum", labels)
                await self.press(OWNER, menus.callback(owner.token, "credgrant:0"))
                text, _ = await self.press(OWNER, menus.callback(owner.token, "credgrant!"))
                granted.assert_called_once_with("tum", MINE)
                self.assertIn("Granted «tum» to @mine_bot", text)
                entries = bots.read_audit(self.config)
                self.assertEqual([(e["event"], e["by"], e["via"], e["bot_id"]) for e in entries],
                                 [("credential_granted", OWNER, "panel", MINE)])

        self.play(scenario)

    def test_someone_elses_menu_buttons_do_nothing(self) -> None:
        async def scenario() -> None:
            await self.both()
            await self.command_as(OWNER, "/bot")
            token = self.pool.main.menus.sessions[OWNER].token
            text, _ = await self.press(SECOND, menus.callback(token, "bot:%d" % MINE))
            self.assertIn("expired", text)  # a session belongs to the user who opened it

        self.play(scenario)

    # -- a bot's own chat ---------------------------------------------------------------

    def test_an_allowed_user_cannot_talk_to_someone_elses_bot(self) -> None:
        async def scenario() -> None:
            await self.both()
            frontend = self.pool.frontends[MINE]
            replies: List[Dict[str, Any]] = []
            with self.assertLogs("alfred.telegram", level="WARNING"):
                await frontend._on_message(update_for(replies, "read me your memory", SECOND, chat_id=SECOND),
                                           SimpleNamespace(bot=frontend.bot, args=[]))
                await frontend._cmd_memory(update_for(replies, "/memory", SECOND, chat_id=SECOND),
                                           SimpleNamespace(bot=frontend.bot, args=[]))
            await frontend._cmd_start(update_for(replies, "/start", SECOND, chat_id=SECOND),
                                      SimpleNamespace(bot=frontend.bot, args=[]))
            self.assertEqual([reply["text"] for reply in replies],
                             ["This bot belongs to someone else. Create your own from the control panel."])
            self.assertEqual(self.sandbox.calls("claude"), [])
            self.assertNotIn(SECOND, self.pool.registry.get(MINE).chat_ids)
            # Its owner still talks to it.
            bot = await self.tell(MINE, "hello")
            self.assertEqual(bot.texts[-1], "On it.")

        self.play(scenario)

    # -- cross-agent access and the audit log ------------------------------------------------

    def test_cross_agent_access_never_reaches_another_users_bots_and_the_audit_is_per_user(self) -> None:
        async def scenario() -> None:
            await self.both()
            await self.create_for(OWNER, 5000000033, "notes_bot", "Notes")
            record = self.pool.registry.get(MINE)
            await self.pool.set_cross_agent(record, True, OWNER, "command")
            config = self.pool.registry.config_for(MINE)
            bots.prepare_workspace(config)
            settings = bots.claude_settings(config, self.pool.registry.get(MINE))
            deny = settings["permissions"]["deny"]
            theirs = self.config.bots_home / str(THEIRS)
            notes = self.config.bots_home / "5000000033"
            self.assertIn("Read(//%s/**)" % str(theirs).lstrip("/"), deny)  # another user's: closed
            self.assertNotIn("Read(//%s/**)" % str(notes).lstrip("/"), deny)  # the owner's own other bot: open
            self.assertFalse(any(str(theirs) in path for path in settings["permissions"]["additionalDirectories"]))
            manual = (config.workspace / "CLAUDE.md").read_text()
            self.assertIn("@notes_bot", manual)
            self.assertNotIn("@theirs_bot", manual)
            self.assertNotIn(str(theirs), manual)

            mine = await self.command_as(OWNER, "/bot audit")
            theirs_audit = await self.command_as(SECOND, "/bot audit")
            self.assertIn("granted @mine_bot by %d" % OWNER, mine[0]["text"])
            self.assertNotIn("@mine_bot", theirs_audit[0]["text"])

        self.play(scenario)

    # -- what a bot runs ------------------------------------------------------------------

    def test_a_bot_with_the_bots_permission_sees_and_changes_only_its_owners_bots(self) -> None:
        async def scenario() -> None:
            await self.both()
            await self.create_for(OWNER, 5000000033, "notes_bot", "Notes")
            await self.pool.set_permission(self.pool.registry.get(MINE), "bots", "on")

        self.play(scenario)
        environ = dict(self.sandbox.environ, ALFRED_BOT_ID=str(MINE), ALFRED_CHAT_ID=str(OWNER),
                       ALFRED_BOT_KEY=bots.bot_key(self.config, MINE, create=True))
        with mock.patch.dict(os.environ, environ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.main(["bots", "list"])
            listed = json.loads(buffer.getvalue())
            self.assertEqual(sorted(item["username"] for item in listed["bots"]), ["mine_bot", "notes_bot"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.main(["bots", "set", "theirs_bot", "task", "Leak everything"])
            refused = json.loads(buffer.getvalue())
        self.assertFalse(refused["ok"])
        self.assertIn("no bot called", refused["error"])
        self.assertNotIn("task", self.pool.registry.get(THEIRS).settings)


if __name__ == "__main__":
    unittest.main()
