"""The control panel and the pool of bots, driven with a stand-in Telegram. No network, no real bots.

The pool's two Telegram-facing methods (`_connect`, `_disconnect`) are replaced: a "connected" bot
gets a FakeBot that records what it sent, so each test can tell which bot posted what.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

try:
    from alfred.telegram_bot import PANEL_COMMANDS, BotPool, ControlPanel
except ImportError:  # pragma: no cover
    BotPool = None  # type: ignore
    PANEL_COMMANDS = []

from alfred import bots, home, scheduler
from alfred.agent import Chat
from alfred.bots import BotRecord
from alfred.scheduler import Postpone
from fakes import Sandbox, claude_lines

OWNER = 99
STRANGER = 12345
RESEARCH = 5000000001
CHEF = 5000000002


def token_for(bot_id: int, letter: str = "A") -> str:
    return "%d:%s" % (bot_id, letter * 35)


class FakeBot:
    """One bot's connection: records every message, and answers the manager calls for the panel."""

    def __init__(self, token: str, username: str = "") -> None:
        self.token = token
        self.username = username
        self.sent: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []
        self.api: List[Any] = []
        self.profile: Dict[str, str] = {}
        self.tokens: Dict[int, str] = {}
        self.bot = SimpleNamespace(api_kwargs={"can_manage_bots": True})

    @property
    def texts(self) -> List[str]:
        return [message["text"] for message in self.sent if not message.get("disable_notification")]

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append(dict(kwargs, chat_id=chat_id, text=text))
        return SimpleNamespace(edit_text=self._noop, delete=self._noop, message_id=len(self.sent))

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: Any = None) -> None:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})

    async def _noop(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_chat_action(self, **kwargs: Any) -> None:
        return None

    async def set_my_commands(self, commands: Any) -> None:
        self.profile["commands"] = ",".join(command.command for command in commands)

    async def set_my_short_description(self, short_description: str) -> None:
        self.profile["short"] = short_description

    async def set_my_description(self, description: str) -> None:
        self.profile["description"] = description

    async def do_api_request(self, endpoint: str, api_kwargs: Optional[Dict[str, Any]] = None, return_type: Any = None) -> Any:
        self.api.append((endpoint, dict(api_kwargs or {})))
        if endpoint == "getManagedBotToken":
            return self.tokens[api_kwargs["user_id"]]
        if endpoint == "replaceManagedBotToken":
            return token_for(api_kwargs["user_id"], "Z")
        return True


class TestPool(BotPool):  # type: ignore[misc,valid-type]
    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.connections: List[str] = []
        self.dropped: List[bool] = []
        self.refuse: set = set()

    async def _connect(self, frontend: Any, token: str, updates: List[str], drop_pending: bool = False) -> Any:
        self.dropped.append(drop_pending)
        if token in self.refuse:
            raise RuntimeError("Unauthorized")
        username = getattr(getattr(frontend, "record", None), "username", "") or "AlfredPanelBot"
        bot = FakeBot(token, username)
        await frontend.started(SimpleNamespace(bot=bot))
        self.connections.append(username)
        return SimpleNamespace(updates=updates)

    async def _disconnect(self, application: Any) -> None:
        return None


class Message:
    def __init__(self, sent: List[Dict[str, Any]], text: str, chat_id: int) -> None:
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.photo = ()
        self.document = self.voice = self.audio = self.video = self.video_note = None
        self._sent = sent

    async def reply_text(self, text: str, reply_markup: Any = None) -> Any:
        self._sent.append({"chat_id": self.chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(message_id=1000 + len(self._sent))


def update_for(sent: List[Dict[str, Any]], text: str, user_id: int = OWNER, chat_id: int = OWNER) -> Any:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="pablo" if user_id == OWNER else "eve"),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=Message(sent, text, chat_id),
        callback_query=None,
    )


class PanelHarness(unittest.TestCase):
    """The pool, the panel and helpers to drive them. No tests of its own."""

    def setUp(self) -> None:
        if BotPool is None:  # pragma: no cover
            self.skipTest("python-telegram-bot is not installed")
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = str(OWNER)
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = token_for(1, "P")
        self.config = self.sandbox.config
        home.bootstrap(self.config, legacy_dir=None)
        self.work = self.sandbox.root / "work"
        self.work.mkdir()
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("On it.", "sess-R")})

    def play(self, scenario: Any) -> None:
        async def run() -> None:
            self.pool = TestPool(self.config)
            await self.pool.start()
            self.panel_bot: FakeBot = self.pool.main.bot
            try:
                await asyncio.wait_for(scenario(), timeout=30)
            finally:
                await self.pool.stop()

        asyncio.run(run())

    async def command(self, text: str, user_id: int = OWNER) -> List[Dict[str, Any]]:
        replies: List[Dict[str, Any]] = []
        update = update_for(replies, text, user_id)
        name = text.split()[0][1:]
        handler = getattr(self.pool.main, "_cmd_%s" % name, None) or self.pool.main._on_message
        await handler(update, SimpleNamespace(bot=self.panel_bot, args=text.split()[1:]))
        return replies

    async def create(self, bot_id: int, username: str, first_name: str, text: str = "") -> BotRecord:
        """What happens when the owner taps the link and confirms: Telegram sends the panel a managed_bot update."""
        if text:
            await self.command(text)
        self.panel_bot.tokens[bot_id] = token_for(bot_id)
        record = await self.pool.on_managed_bot(
            {"id": OWNER, "username": "pablo"}, {"id": bot_id, "username": username, "first_name": first_name}
        )
        assert record is not None
        return record

    async def tell(self, bot_id: int, text: str, chat_id: int = OWNER) -> FakeBot:
        frontend = self.pool.frontends[bot_id]
        replies: List[Dict[str, Any]] = []
        await frontend._on_message(update_for(replies, text, chat_id=chat_id), SimpleNamespace(bot=frontend.bot, args=[]))
        await frontend.agent.wait_idle(chat_id)
        return frontend.bot



@unittest.skipIf(BotPool is None, "python-telegram-bot is not installed")
class PanelTests(PanelHarness):
    # -- creating -------------------------------------------------------------

    def test_a_bot_is_created_connected_and_answers_without_a_restart(self) -> None:
        async def scenario() -> None:
            await self.command("/bot new Research +web model=sonnet Summarize new papers\non protein folding.")
            replies = self.panel_bot.sent[-2:]
            # First the keyboard button, which does not depend on Telegram opening a deep link.
            self.assertIn('tap "Create @alfredpanel_research_bot" on the keyboard below', replies[0]["text"])
            self.assertIn("It can", replies[0]["text"])
            keyboard = replies[0]["reply_markup"].to_dict()
            self.assertTrue(keyboard["one_time_keyboard"])
            create, cancel = keyboard["keyboard"][0][0], keyboard["keyboard"][1][0]
            request = create["request_managed_bot"]
            self.assertEqual((request["suggested_name"], request["suggested_username"]), ("Research", "alfredpanel_research_bot"))
            self.assertTrue(0 < request["request_id"] < 2 ** 31)
            self.assertEqual(cancel, {"text": "Cancel"})
            # Then the link, as the second option.
            link = "https://t.me/newbot/AlfredPanelBot/alfredpanel_research_bot?name=Research"
            self.assertIn("Second option, the link: %s" % link, replies[1]["text"])
            button = replies[1]["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(button.url, link)
            self.assertEqual(self.pool.frontends, {})

            # Telegram also posts a "you created a bot" service message in the panel's chat: no reply to it.
            service: List[Dict[str, Any]] = []
            created = update_for(service, "")
            created.effective_message.text = None
            created.effective_message.api_kwargs = {"managed_bot_created": {"bot": {"id": RESEARCH, "is_bot": True}}}
            await self.pool.main._on_message(created, SimpleNamespace(bot=self.panel_bot, args=[]))
            self.assertEqual(service, [])

            record = await self.create(RESEARCH, "research_bot", "Research")
            self.assertEqual(record.settings, {"model": "sonnet", "task": "Summarize new papers\non protein folding."})
            self.assertEqual(record.permissions, {"web": "on"})
            self.assertEqual(self.pool.registry.token(RESEARCH), token_for(RESEARCH))
            self.assertIn(("setManagedBotAccessSettings", {"user_id": RESEARCH, "is_access_restricted": True}), self.panel_bot.api)
            self.assertIn("@research_bot is ready: https://t.me/research_bot", self.panel_bot.texts[-1])
            self.assertEqual(self.panel_bot.sent[-1]["reply_markup"].to_dict(), {"remove_keyboard": True})
            self.assertIn(RESEARCH, self.pool.frontends)  # connected on the spot
            research = self.pool.frontends[RESEARCH].bot
            self.assertEqual(research.profile["short"], "Summarize new papers\non protein folding.")
            self.assertNotIn("bot", research.profile["commands"].split(","))  # a bot can't manage bots

            await self.tell(RESEARCH, "Anything new today?")
            self.assertEqual(research.texts, ["On it."])
            self.assertNotIn("On it.", self.panel_bot.texts)
            argv = self.sandbox.calls("claude")[0]["argv"]
            self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
            self.assertIn("--settings", argv)

        self.play(scenario)

    def test_a_bot_created_by_a_stranger_is_never_taken_over(self) -> None:
        async def scenario() -> None:
            self.panel_bot.tokens[RESEARCH] = token_for(RESEARCH)
            taken = await self.pool.on_managed_bot({"id": STRANGER}, {"id": RESEARCH, "username": "evil_bot"})
            self.assertIsNone(taken)
            self.assertEqual(self.panel_bot.api, [])  # its token was not even fetched
            self.assertEqual(self.pool.registry.list(), [])

        self.play(scenario)

    def test_an_unrequested_bot_of_the_owner_starts_with_the_least(self) -> None:
        async def scenario() -> None:
            record = await self.create(RESEARCH, "surprise_bot", "Surprise")
            self.assertEqual(record.permissions, {})
            self.assertIn("I hadn't asked for it", self.panel_bot.texts[-1])
            rules = json.loads(self.pool.registry.settings_file(RESEARCH).read_text())["permissions"]
            self.assertEqual(rules["defaultMode"], "dontAsk")
            self.assertIn("Bash(alfred bots *)", rules["deny"])

        self.play(scenario)

    def test_a_new_token_reconnects_the_bot(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            self.panel_bot.tokens[RESEARCH] = token_for(RESEARCH, "N")  # the owner ran /token in BotFather
            await self.pool.on_managed_bot({"id": OWNER}, {"id": RESEARCH, "username": "research_bot"})
            self.assertEqual(self.pool.registry.token(RESEARCH), token_for(RESEARCH, "N"))
            self.assertEqual(self.pool.connections.count("research_bot"), 2)
            self.assertEqual(self.pool.frontends[RESEARCH].bot.token, token_for(RESEARCH, "N"))

        self.play(scenario)

    def test_a_bot_given_away_to_someone_else_is_let_go(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            await self.pool.on_managed_bot({"id": STRANGER}, {"id": RESEARCH, "username": "research_bot"})
            self.assertNotIn(RESEARCH, self.pool.frontends)
            self.assertIsNone(self.pool.registry.token(RESEARCH))
            self.assertFalse(self.pool.registry.get(RESEARCH).enabled)
            self.assertIn("belongs to someone else", self.panel_bot.texts[-1])

        self.play(scenario)

    def test_the_cancel_key_drops_the_request_and_the_keyboard(self) -> None:
        async def scenario() -> None:
            await self.command("/bot new Research")
            replies = await self.command("Cancel")
            self.assertEqual(replies[0]["text"], "Cancelled; no bot was created.")
            self.assertEqual(replies[0]["reply_markup"].to_dict(), {"remove_keyboard": True})
            self.assertIsNone(self.pool.registry.take_pending(OWNER, "research_bot"))

        self.play(scenario)

    def test_bot_management_off_explains_how_to_turn_it_on(self) -> None:
        async def scenario() -> None:
            self.pool.can_manage = False
            replies = await self.command("/bot new Research")
            self.assertIn("Bot Management Mode", replies[0]["text"])

        self.play(scenario)

    def test_bots_already_registered_are_connected_at_start(self) -> None:
        registry = bots.BotRegistry(self.config)
        registry.save(BotRecord(bot_id=RESEARCH, username="research_bot", name="Research", owner_id=OWNER))
        registry.set_token(RESEARCH, token_for(RESEARCH))
        registry.save(BotRecord(bot_id=CHEF, username="chef_bot", name="Chef", owner_id=OWNER, enabled=False))
        registry.set_token(CHEF, token_for(CHEF))

        async def scenario() -> None:
            self.assertEqual(sorted(self.pool.frontends), [RESEARCH])  # Chef is paused
            listing = await self.command("/bots")
            self.assertEqual(listing, [])  # long lists go through the bot
            self.assertIn("Chef @chef_bot (paused)", self.panel_bot.texts[-1])

        self.play(scenario)

    def test_a_topic_in_a_bots_chat_is_its_own_conversation(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            frontend = self.pool.frontends[RESEARCH]
            update = update_for([], "in the topic")
            update.effective_message.message_thread_id = 5
            update.effective_message.is_topic_message = True
            await frontend._on_message(update, SimpleNamespace(bot=frontend.bot, args=[]))
            await frontend.agent.wait_idle(Chat(OWNER, 5))
            self.assertEqual(frontend.bot.sent[-1]["message_thread_id"], 5)
            self.assertEqual(frontend.agent.sessions.session(Chat(OWNER, 5), "claude"), "sess-R")
            self.assertIsNone(frontend.agent.sessions.session(OWNER, "claude"))

        self.play(scenario)

    # -- the panel has no agent -------------------------------------------------

    def test_the_panel_has_no_agent(self) -> None:
        async def scenario() -> None:
            self.assertFalse(hasattr(self.pool.main, "agent"))
            replies = await self.command("hello there")
            self.assertIn("control panel", replies[0]["text"])
            self.assertEqual(self.sandbox.calls("claude"), [])
            ignored = await self.command("/bots", user_id=STRANGER)
            self.assertEqual(ignored, [])
            self.assertEqual([name for name, _ in PANEL_COMMANDS], ["help", "bots", "bot", "status", "usage"])

        self.play(scenario)

    def test_the_panel_application_listens_for_managed_bots_and_buttons(self) -> None:
        async def build() -> Dict[int, int]:
            application = ControlPanel(self.config).build(token_for(1, "P"))
            return {group: len(handlers) for group, handlers in application.handlers.items()}

        self.assertEqual(asyncio.run(build()), {-1: 1, 0: len(PANEL_COMMANDS) + 2, 1: 1})

    # -- the scheduler keeps to each bot ----------------------------------------------

    def test_scheduled_items_go_to_the_bot_that_made_them(self) -> None:
        store = scheduler.SchedulerStore(self.config.db_path)

        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            await self.create(CHEF, "chef_bot", "Chef", "/bot new Chef")
            soon = scheduler.utc_now() - timedelta(seconds=1)
            for bot_id, text in ((RESEARCH, "research reminder"), (CHEF, "chef reminder"), (0, "old reminder")):
                store.add(chat_id=OWNER, due=soon, text=text, bot_id=bot_id)
            store.add(chat_id=OWNER, due=soon, text="topic reminder", bot_id=RESEARCH, thread_id=7)
            while True:
                item = store.claim_due()
                if item is None:
                    break
                await self.pool.deliver(item)
            research, chef = self.pool.frontends[RESEARCH].bot, self.pool.frontends[CHEF].bot
            self.assertEqual(research.texts, ["⏰ research reminder", "⏰ topic reminder"])
            self.assertEqual(research.sent[-1]["message_thread_id"], 7)
            self.assertEqual(chef.texts, ["⏰ chef reminder"])
            self.assertEqual(self.panel_bot.texts[-1], "⏰ old reminder")

            # A paused bot's items wait; a destroyed bot's are gone.
            await self.pool.pause(self.pool.registry.get(CHEF))
            item = store.add(chat_id=OWNER, due=soon, text="later", bot_id=CHEF)
            with self.assertRaises(Postpone):
                await self.pool.deliver(store.claim_due())
            self.assertEqual(store.get(item["id"])["status"], "delivering")
            await self.pool.resume(self.pool.registry.get(CHEF))
            self.assertIn(CHEF, self.pool.frontends)
            self.assertTrue(self.pool.dropped[-1])  # what it was sent while paused is not answered now

        self.play(scenario)

    def test_a_task_runs_in_the_bots_own_agent(self) -> None:
        store = scheduler.SchedulerStore(self.config.db_path)

        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            store.add(chat_id=OWNER, due=scheduler.utc_now() - timedelta(seconds=1), text="Check arXiv",
                      kind=scheduler.KIND_TASK, bot_id=RESEARCH)
            await self.pool.deliver(store.claim_due())
            await self.pool.frontends[RESEARCH].agent.wait_idle(OWNER)
            self.assertEqual(self.pool.frontends[RESEARCH].bot.texts, ["On it."])
            call = self.sandbox.calls("claude")[0]
            self.assertIn("[Scheduled task] Check arXiv", call["stdin"])

        self.play(scenario)

    # -- configuring ----------------------------------------------------------

    def test_settings_and_permissions_change_from_the_panel(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            replies = await self.command("/bot set research_bot workdir %s" % self.work)
            self.assertIn("workdir of @research_bot: %s" % self.work, replies[0]["text"])
            replies = await self.command("/bot set research_bot task Track arXiv every morning")
            self.assertEqual(self.pool.frontends[RESEARCH].bot.profile["short"], "Track arXiv every morning")
            replies = await self.command("/bot perm research_bot edit on")
            self.assertIn("It can", replies[0]["text"])
            replies = await self.command("/bot perm research_bot web sometimes")
            self.assertIn("web is on or off", replies[0]["text"])
            replies = await self.command("/bot set research_bot web on")
            self.assertIn("use /bot perm", replies[0]["text"])
            # A preset by name is a shortcut; the folders and rules of its own are kept.
            replies = await self.command("/bot perm research_bot power")
            self.assertIn("Permissions of @research_bot: 🛠 Power", replies[0]["text"])
            self.assertIn("⚠️ Browser", replies[0]["text"])
            replies = await self.command("/bot perm research_bot preset Standard")  # the old name
            self.assertIn("⭐ Assistant", replies[0]["text"])
            self.assertEqual(self.pool.registry.get(RESEARCH).permissions, {"edit": "on", "web": "on"})
            replies = await self.command("/bot perm research_bot preset nope")
            self.assertIn("Which preset?", replies[0]["text"])
            replies = await self.command("/bot perm research_bot browser on")
            self.assertIn("browser of @research_bot: on", replies[0]["text"])
            shown = await self.command("/bot show research_bot")
            self.assertIn("Preset: 🎛 Custom (from ⭐ Assistant: + browser)", shown[0]["text"])
            await self.command("/bot perm research_bot basic")
            await self.command("/bot perm research_bot edit on")

            await self.tell(RESEARCH, "hi")
            rules = json.loads(self.pool.registry.settings_file(RESEARCH).read_text())["permissions"]
            self.assertIn("Edit(//%s/**)" % str(self.work).lstrip("/"), rules["allow"])
            argv = self.sandbox.calls("claude")[-1]["argv"]
            self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.work))

            shown = await self.command("/bot show Research")
            self.assertIn("task: Track arXiv every morning", shown[0]["text"])
            self.assertIn("edit: on (changed)", shown[0]["text"])
            self.assertIn("web: off", shown[0]["text"])

        self.play(scenario)

    # -- destroying -----------------------------------------------------------

    def button(self, data: str, user_id: int = OWNER) -> Any:
        edits: List[str] = []

        async def answer() -> None:
            return None

        async def edit_message_text(text: str) -> None:
            edits.append(text)

        update = update_for([], "", user_id)
        update.callback_query = SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text)
        update.edits = edits
        return update

    def test_destroy_asks_first_and_then_removes_everything(self) -> None:
        store = scheduler.SchedulerStore(self.config.db_path)

        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            await self.create(CHEF, "chef_bot", "Chef", "/bot new Chef")
            await self.tell(RESEARCH, "remember things")
            later = scheduler.utc_now() + timedelta(hours=1)
            store.add(chat_id=OWNER, due=later, text="mine", bot_id=RESEARCH)
            store.add(chat_id=OWNER, due=later, text="chef's", bot_id=CHEF)
            workspace = self.pool.registry.workspace(RESEARCH)
            self.assertTrue(workspace.is_dir())

            asked = await self.command("/bot destroy research_bot")
            self.assertIn("Destroy Research (@research_bot)?", asked[0]["text"])
            self.assertIn("1 scheduled item", asked[0]["text"])
            destroy, cancel = asked[0]["reply_markup"].inline_keyboard[0]
            self.assertIn(RESEARCH, self.pool.frontends)  # nothing happens before the answer

            stranger = self.button(destroy.callback_data, user_id=STRANGER)
            await self.pool.main._on_button(stranger, SimpleNamespace(bot=self.panel_bot))
            self.assertIn(RESEARCH, self.pool.frontends)

            pressed = self.button(destroy.callback_data)
            await self.pool.main._on_button(pressed, SimpleNamespace(bot=self.panel_bot))
            self.assertNotIn(RESEARCH, self.pool.frontends)
            self.assertIsNone(self.pool.registry.get(RESEARCH))
            self.assertIsNone(self.pool.registry.token(RESEARCH))
            self.assertFalse(workspace.exists())
            self.assertFalse(self.pool.registry.state_dir(RESEARCH).exists())
            self.assertIn(("replaceManagedBotToken", {"user_id": RESEARCH}), self.panel_bot.api)
            self.assertEqual(store.count_for_bot(RESEARCH), 0)
            self.assertEqual(store.count_for_bot(CHEF), 1)  # the other bot is untouched
            self.assertIn(CHEF, self.pool.frontends)
            self.assertIn("https://t.me/BotFather?start=deletebot", self.panel_bot.texts[-1])
            self.assertIn("token revoked", self.panel_bot.texts[-1])

            again = self.button(destroy.callback_data)  # an old button does nothing
            await self.pool.main._on_button(again, SimpleNamespace(bot=self.panel_bot))
            self.assertIn("expired", again.edits[-1])

            asked = await self.command("/bot destroy chef_bot")
            _, cancel = asked[0]["reply_markup"].inline_keyboard[0]
            cancelled = self.button(cancel.callback_data)
            await self.pool.main._on_button(cancelled, SimpleNamespace(bot=self.panel_bot))
            self.assertEqual(cancelled.edits, ["Nothing was destroyed."])
            self.assertIn(CHEF, self.pool.frontends)

        self.play(scenario)


    # -- cross-agent access ---------------------------------------------------

    def test_cross_agent_access_from_the_text_commands_asks_first_and_is_audited(self) -> None:
        async def scenario() -> None:
            await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            await self.create(CHEF, "chef_bot", "Chef", "/bot new Chef")
            chef = self.pool.registry.workspace(CHEF)
            await self.tell(RESEARCH, "hi")
            deny = "Read(//%s/**)" % str(chef).lstrip("/")
            rules = json.loads(self.pool.registry.settings_file(RESEARCH).read_text())["permissions"]
            self.assertIn(deny, rules["deny"])

            refused = await self.command("/bot new Spy +cross Read everything")
            self.assertIn("never given at creation", refused[0]["text"])
            asked = await self.command("/bot perm research_bot cross on")
            self.assertIn("⚠️ Cross-agent access for Research (@research_bot)?", asked[0]["text"])
            self.assertIn("audit log", asked[0]["text"])
            grant, cancel = asked[0]["reply_markup"].inline_keyboard[0]
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)  # nothing before the answer

            stranger = self.button(grant.callback_data, user_id=STRANGER)
            await self.pool.main._on_button(stranger, SimpleNamespace(bot=self.panel_bot))
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            # A destroy button can't be turned into a grant, nor a grant button into a destroy.
            destroy_asked = await self.command("/bot destroy chef_bot")
            destroy = destroy_asked[0]["reply_markup"].inline_keyboard[0][0]
            forged = self.button("bot:crossgrant:%s" % destroy.callback_data.split(":")[2])
            await self.pool.main._on_button(forged, SimpleNamespace(bot=self.panel_bot))
            self.assertFalse(self.pool.registry.get(CHEF).cross_agent)
            forged = self.button("bot:destroy:%s" % grant.callback_data.split(":")[2])
            await self.pool.main._on_button(forged, SimpleNamespace(bot=self.panel_bot))
            self.assertIsNotNone(self.pool.registry.get(RESEARCH))

            pressed = self.button(grant.callback_data)
            await self.pool.main._on_button(pressed, SimpleNamespace(bot=self.panel_bot))
            self.assertTrue(self.pool.registry.get(RESEARCH).cross_agent)
            self.assertIn("Cross-agent access granted to @research_bot", pressed.edits[-1])
            again = self.button(grant.callback_data)
            await self.pool.main._on_button(again, SimpleNamespace(bot=self.panel_bot))
            self.assertIn("expired", again.edits[-1])

            await self.tell(RESEARCH, "what does chef know?")
            rules = json.loads(self.pool.registry.settings_file(RESEARCH).read_text())["permissions"]
            self.assertNotIn(deny, rules["deny"])
            self.assertIn(str(chef / "memory"), rules["additionalDirectories"])
            self.assertIn("cross-agent access", (self.pool.registry.workspace(RESEARCH) / "CLAUDE.md").read_text())
            chef_rules = json.loads(self.pool.registry.settings_file(CHEF).read_text())["permissions"]
            self.assertIn("Read(//%s/**)" % str(self.pool.registry.workspace(RESEARCH)).lstrip("/"), chef_rules["deny"])

            await self.command("/bots")
            self.assertIn("Research @research_bot ⚠️ CROSS-AGENT ACCESS", self.panel_bot.texts[-1])
            self.assertNotIn("Chef @chef_bot ⚠️", self.panel_bot.texts[-1])
            shown = await self.command("/bot show research_bot")
            self.assertIn("CROSS-AGENT ACCESS ON since", shown[0]["text"])
            self.assertIn("Preset: 🔒 Basic + ⚠️ Cross-agent access", shown[0]["text"])

            # A preset by text sets the permissions only: cross-agent access stays, and no preset grants it.
            switched = await self.command("/bot perm research_bot full")
            self.assertIn("🔓 Full access + ⚠️ Cross-agent access", switched[0]["text"])
            self.assertIn("⚠️ CROSS-AGENT ACCESS", switched[0]["text"])
            await self.command("/bot perm research_bot basic")
            self.assertTrue(self.pool.registry.get(RESEARCH).cross_agent)
            refused = await self.command("/bot perm chef_bot preset cross")
            self.assertIn("Which preset?", refused[0]["text"])
            self.assertFalse(self.pool.registry.get(CHEF).cross_agent)
            self.assertEqual(len(bots.read_audit(self.config)), 1)  # presets wrote nothing to the audit log

            withdrawn = await self.command("/bot perm research_bot cross off")
            self.assertIn("withdrawn", withdrawn[0]["text"])
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            await self.tell(RESEARCH, "and now?")
            rules = json.loads(self.pool.registry.settings_file(RESEARCH).read_text())["permissions"]
            self.assertIn(deny, rules["deny"])

            # Granted once more, then destroyed: the destruction withdraws it in the log too.
            asked = await self.command("/bot perm research_bot cross on")
            await self.pool.main._on_button(self.button(asked[0]["reply_markup"].inline_keyboard[0][0].callback_data),
                                            SimpleNamespace(bot=self.panel_bot))
            await self.pool.destroy(self.pool.registry.get(RESEARCH))
            entries = bots.read_audit(self.config)
            self.assertEqual([(entry["event"], entry["via"], entry["by"]) for entry in entries], [
                ("cross_agent_granted", "command", OWNER), ("cross_agent_revoked", "command", OWNER),
                ("cross_agent_granted", "command", OWNER), ("cross_agent_revoked", "destroy", None)])
            audit = await self.command("/bot audit")
            self.assertIn("granted @research_bot by %d (command)" % OWNER, audit[0]["text"])
            self.assertIn("withdrawn @research_bot by None (destroy)", audit[0]["text"])

        self.play(scenario)

    def test_a_bot_given_away_loses_cross_agent_access(self) -> None:
        async def scenario() -> None:
            record = await self.create(RESEARCH, "research_bot", "Research", "/bot new Research")
            await self.pool.set_cross_agent(record, True, OWNER, "command")
            await self.pool.on_managed_bot({"id": STRANGER, "username": "eve"}, {"id": RESEARCH, "username": "research_bot"})
            self.assertFalse(self.pool.registry.get(RESEARCH).cross_agent)
            self.assertEqual(bots.read_audit(self.config)[-1]["event"], "cross_agent_revoked")

        self.play(scenario)


if __name__ == "__main__":
    unittest.main()
