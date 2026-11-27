"""The Telegram layer, driven with stand-in update objects. Needs python-telegram-bot installed.

A message handler returns as soon as the agent has the message, so each test
runs as one scenario that waits for the chat to go idle before looking at what
was sent, and shuts the agent's processes down at the end.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
import os
from typing import Any, Awaitable, Callable, List, Optional

try:
    from consiglieri.telegram_bot import COMMANDS, TelegramBot
except ImportError:  # pragma: no cover
    TelegramBot = None  # type: ignore
    COMMANDS = []

from consiglieri import home, scheduler
from fakes import Sandbox, claude_lines


class FakeBot:
    def __init__(self) -> None:
        self.sent: List[str] = []
        self.status: List[str] = []

    async def send_message(self, chat_id: int, text: str, disable_notification: bool = False) -> Any:
        if disable_notification:  # the progress message
            self.status.append(text)
            return FakeStatusMessage(self)
        self.sent.append(text)

    async def send_chat_action(self, chat_id: int, action: Any) -> None:
        pass


class FakeStatusMessage:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot

    async def edit_text(self, text: str) -> None:
        self.bot.status.append(text)

    async def delete(self) -> None:
        self.bot.status.append("<deleted>")


class FakeFile:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_to_drive(self, custom_path: str) -> None:
        with open(custom_path, "wb") as handle:
            handle.write(self.content)


class FakeDocument:
    def __init__(self, file_name: str, content: bytes = b"data") -> None:
        self.file_name = file_name
        self.content = content

    async def get_file(self) -> FakeFile:
        return FakeFile(self.content)


def make_update(bot: FakeBot, text: Optional[str] = None, *, user_id: int = 99, username: Optional[str] = "pablo",
                chat_id: int = 7, document: Any = None, caption: Optional[str] = None) -> Any:
    async def reply_text(reply: str) -> None:
        bot.sent.append(reply)

    message = SimpleNamespace(
        text=text, caption=caption, chat_id=chat_id, photo=(), document=document, voice=None, audio=None,
        video=None, video_note=None, reply_text=reply_text,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=username),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
    )


@unittest.skipIf(TelegramBot is None, "python-telegram-bot is not installed")
class TelegramBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "pablo"
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
        self.record, self.config = self.sandbox.add_bot()
        self.bot = FakeBot()
        self.apps: List[Any] = []
        self.app = self.new_app()

    def new_app(self) -> Any:
        app = TelegramBot(self.config, record=self.record)
        app.bot = self.bot
        self.apps.append(app)
        return app

    def context(self, *args: str) -> Any:
        return SimpleNamespace(bot=self.bot, args=list(args))

    def play(self, scenario: Callable[[], Awaitable[None]]) -> None:
        async def run() -> None:
            try:
                await asyncio.wait_for(scenario(), timeout=30)
            finally:
                for app in self.apps:
                    await app.agent.shutdown()

        asyncio.run(run())

    async def say(self, text: Optional[str] = None, app: Any = None, **update: Any) -> None:
        """Send a chat message and wait until the agent has nothing left to do for that chat."""
        app = app or self.app
        await app._on_message(make_update(self.bot, text, **update), self.context())
        await app.agent.wait_idle(update.get("chat_id", 7))

    def test_application_builds_with_all_handlers(self) -> None:
        async def build() -> int:
            return len(self.app.build("424242:%s" % ("A" * 35)).handlers[0])

        # /start, every entry in COMMANDS, and the catch-all message handler.
        self.assertEqual(asyncio.run(build()), len(COMMANDS) + 2)

    def test_every_listed_command_has_a_handler_and_a_menu_sized_description(self) -> None:
        for name, description in COMMANDS:
            self.assertTrue(callable(getattr(self.app, "_cmd_%s" % name, None)), name)
            self.assertRegex(name, r"^[a-z0-9_]{1,32}$")  # Telegram's rule for command names
            self.assertLessEqual(len(description), 256)

    def test_help_lists_every_command(self) -> None:
        asyncio.run(self.app._cmd_help(make_update(self.bot, "/help"), self.context()))
        text = self.bot.sent[0]
        for name, description in COMMANDS:
            self.assertIn("/%s - %s" % (name, description), text)
        self.assertIn("Write to me in plain language", text)
        self.assertIn("I'm Helper", text)

    def test_usage_command_replies_with_the_short_report(self) -> None:
        report = (
            "You are currently using your subscription to power your Claude Code usage\n\n"
            "Current session: 70% used · resets Nov 11 at 7:09pm (Europe/Berlin)\n"
            "Current week (all models): 15% used · resets Nov 15 at 5:59am (Europe/Berlin)\n\n"
            "What's contributing to your usage?\n  55%  Start fresh when you switch tasks"
        )
        self.sandbox.fake_cli("claude", {"fresh": [{"type": "result", "is_error": False, "result": report}]})

        asyncio.run(self.app._cmd_usage(make_update(self.bot, "/usage"), self.context()))

        self.assertEqual(
            self.bot.sent,
            [
                "Claude usage\n\n"
                "Session: 70% used\n▓▓▓▓▓▓▓░░░ resets Nov 11 at 7:09pm\n\n"
                "Week (all models): 15% used\n▓▓░░░░░░░░ resets Nov 15 at 5:59am"
            ],
        )
        call = self.sandbox.calls("claude")[0]
        self.assertEqual(call["stdin"], "/usage")
        self.assertNotIn("--resume", call["argv"])
        self.assertIn("--no-session-persistence", call["argv"])

    def test_message_from_allowed_user_gets_the_agent_reply(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Good evening.", "sess-A", tool=True)})

        async def scenario() -> None:
            await self.say("hello")

        self.play(scenario)

        self.assertEqual(self.bot.sent, ["Good evening."])
        self.assertEqual(self.bot.status, ["⏳ Bash: List files", "<deleted>"])
        call = self.sandbox.calls("claude")[0]
        self.assertTrue(call["stdin"].startswith('<context source="telegram" bot="@helper_bot" chat_id="7"'))
        self.assertTrue(call["stdin"].endswith("hello"))
        self.assertTrue(call["live"])

    def test_the_process_stays_open_between_messages_and_each_chat_has_its_own(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-A")})
        pids: List[Optional[int]] = []

        async def scenario() -> None:
            await self.say("one")
            pids.append(self.app.agent.live_pid(7))
            await self.say("two")
            pids.append(self.app.agent.live_pid(7))
            await self.say("other chat", chat_id=8)
            pids.append(self.app.agent.live_pid(8))
            await self.app._cmd_status(make_update(self.bot, "/status"), self.context())

        self.play(scenario)

        self.assertIsNotNone(pids[0])
        self.assertEqual(pids[0], pids[1])
        self.assertNotEqual(pids[0], pids[2])
        self.assertEqual(self.bot.sent[:3], ["Hi.", "Hi.", "Hi."])
        self.assertIn("Process: open, pid %s" % pids[0], self.bot.sent[3])

    def test_a_final_message_of_no_reply_posts_nothing(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("NO_REPLY", "sess-A")})

        async def scenario() -> None:
            await self.say("send me the report with alfred send")
            self.sandbox.fake_cli("claude", {"fresh": claude_lines("`no_reply`.", "sess-A")})
            await self.say("thanks")

        self.play(scenario)
        self.assertEqual(self.bot.sent, [])

    def test_the_agent_can_post_when_it_wakes_up_by_itself(self) -> None:
        woke = claude_lines("The agent in mus-solver finished: tests pass.", "sess-A")
        self.sandbox.fake_cli(
            "claude", {"fresh": claude_lines("Launched.", "sess-A") + [{"_later": woke, "_after": 0.3}]}
        )

        async def scenario() -> None:
            await self.say("launch an agent and tell me when it is done")
            for _ in range(100):
                if len(self.bot.sent) == 2:
                    break
                await asyncio.sleep(0.05)

        self.play(scenario)
        self.assertEqual(self.bot.sent, ["Launched.", "The agent in mus-solver finished: tests pass."])

    def test_strangers_are_ignored_everywhere_except_start(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("secret", "sess-A")})
        stranger = dict(user_id=5, username="mallory")

        async def scenario() -> None:
            with self.assertLogs("consiglieri.telegram", level="WARNING"):
                await self.say("hello", **stranger)
                await self.app._cmd_skills(make_update(self.bot, "/skills", **stranger), self.context())
                await self.app._cmd_new(make_update(self.bot, "/new", **stranger), self.context())
                await self.app._cmd_stop(make_update(self.bot, "/stop", **stranger), self.context())
                await self.app._cmd_backend(make_update(self.bot, "/backend codex", **stranger), self.context("codex"))
            await self.app._cmd_start(make_update(self.bot, "/start", **stranger), self.context())

        self.play(scenario)

        self.assertEqual(self.bot.sent, ["This is a private bot. Your Telegram user id is 5."])
        self.assertEqual(self.sandbox.calls("claude"), [])
        self.assertEqual(self.app.agent.backend_name(7), "claude")

    def test_user_id_allow_list_works_without_a_username(self) -> None:
        self.sandbox.environ["ALFRED_ALLOWED_USERS"] = "99"
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-A")})

        async def scenario() -> None:
            await self.say("hello", username=None)

        self.play(scenario)
        self.assertEqual(self.bot.sent, ["Hi."])

    def test_attachment_is_saved_in_the_inbox_and_named_in_the_prompt(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Got it.", "sess-A")})

        async def scenario() -> None:
            await self.say(document=FakeDocument("../../etc/my report.pdf", b"%PDF"), caption="summarize")

        self.play(scenario)

        saved = list((self.config.inbox_dir / "7").iterdir())
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].name.endswith("-my_report.pdf"), saved[0].name)
        self.assertEqual(saved[0].read_bytes(), b"%PDF")
        prompt = self.sandbox.calls("claude")[0]["stdin"]
        self.assertIn("summarize\n\n[The user attached a file, saved at %s]" % saved[0], prompt)

    def test_long_replies_are_split_and_errors_are_flagged(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("word " * 1500, "sess-A")})

        async def scenario() -> None:
            await self.say("long")
            self.assertEqual(len(self.bot.sent), 2)
            self.assertTrue(all(len(chunk) <= 4096 for chunk in self.bot.sent))

            self.bot.sent.clear()
            await self.app.agent.stop(7)
            self.sandbox.environ["ALFRED_CLAUDE_BIN"] = str(self.sandbox.root / "missing")
            await self.say("hi")

        self.play(scenario)
        self.assertTrue(self.bot.sent[0].startswith("⚠️ claude backend failed"))

    def test_conversation_resumes_across_bot_restarts_and_says_so_when_it_cannot(self) -> None:
        from fakes import CLAUDE_SESSION_GONE

        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hello.", "sess-A")})

        async def scenario() -> None:
            await self.say("first")
            await self.app.agent.shutdown()

            restarted = self.new_app()  # a new process: nothing kept in memory
            await self.say("second", app=restarted)
            argv = self.sandbox.calls("claude")[1]["argv"]
            self.assertEqual(argv[argv.index("--resume") + 1], "sess-A")
            self.assertEqual(self.bot.sent, ["Hello.", "Hello."])
            await restarted.agent.shutdown()

            # Claude Code no longer has that conversation (for example, it was pruned).
            self.sandbox.fake_cli(
                "claude",
                {"fresh": claude_lines("Hello again.", "sess-B"), "resume": CLAUDE_SESSION_GONE, "resume_exit": 1},
            )
            again = self.new_app()
            await self.say("third", app=again)
            self.assertEqual(again.agent.sessions.session(7, "claude"), "sess-B")

        self.play(scenario)
        self.assertTrue(self.bot.sent[-1].startswith("Hello again.\n\n(I couldn't reopen our earlier conversation"))
        self.assertTrue(self.sandbox.calls("claude")[-1]["stdin"].endswith("third"))

    def test_memory_command_and_the_note_when_a_turn_changes_memory(self) -> None:
        write = (
            "mkdir -p memory/preferences && printf -- '---\\nname: drinks\\ndescription: What the user drinks\\n---\\n"
            "- Mate, not coffee\\n' > memory/preferences/drinks.md"
        )
        lines = claude_lines("Mate it is.", "sess-A")
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_run": write}] + lines[1:]})

        async def scenario() -> None:
            await self.app._cmd_memory(make_update(self.bot, "/memory"), self.context())
            await self.say("I drink mate, not coffee")
            await self.app._cmd_memory(make_update(self.bot, "/memory"), self.context())
            # Saved silently: the note is posted even when the agent has nothing to say.
            self.sandbox.fake_cli(
                "claude",
                {"fresh": lines[:1] + [{"_run": "echo '- Sometimes tea' >> memory/preferences/drinks.md"}]
                 + claude_lines("NO_REPLY", "sess-A")[1:]},
            )
            await self.say("and sometimes tea")

        self.play(scenario)

        self.assertIn("Nothing memorized yet", self.bot.sent[0])
        self.assertEqual(self.bot.sent[1], "Mate it is.\n\n💾 Memory: saved preferences/drinks")
        self.assertIn("1 memory file(s)", self.bot.sent[2])
        self.assertIn("• preferences/drinks\nWhat the user drinks", self.bot.sent[2])
        self.assertEqual(self.bot.sent[3], "💾 Memory: updated preferences/drinks")

    def test_stop_kills_the_process_and_the_conversation_picks_up_again(self) -> None:
        lines = claude_lines("Back.", "sess-A")
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_sleep": 30}]})

        async def scenario() -> None:
            await self.app._on_message(make_update(self.bot, "long job"), self.context())
            for _ in range(100):
                if self.app.agent.sessions.session(7, "claude"):
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(self.app.agent.is_busy(7))
            await self.app._cmd_stop(make_update(self.bot, "/stop"), self.context())
            self.assertFalse(self.app.agent.is_busy(7))
            with self.assertRaises(ProcessLookupError):
                os.kill(self.sandbox.live_pid("claude"), 0)
            await self.app._cmd_stop(make_update(self.bot, "/stop"), self.context())

            self.sandbox.fake_cli("claude", {"fresh": lines})
            await self.say("are you there?")

        self.play(scenario)

        self.assertEqual(self.bot.sent, ["Stopped.", "Nothing is running.", "Back."])
        argv = self.sandbox.calls("claude")[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-A")

    def test_commands(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-A")})
        self.app.store.add(chat_id=7, due=scheduler.utc_now() + timedelta(hours=1), text="Call Sam", bot_id=self.record.bot_id)

        async def scenario() -> None:
            await self.say("hello")
            await self.app._cmd_status(make_update(self.bot, "/status"), self.context())
            await self.app._cmd_skills(make_update(self.bot, "/skills"), self.context())
            await self.app._cmd_schedule(make_update(self.bot, "/schedule"), self.context())
            await self.app._cmd_backend(make_update(self.bot, "/backend"), self.context())
            await self.app._cmd_backend(make_update(self.bot, "/backend x"), self.context("gemini"))
            await self.app._cmd_backend(make_update(self.bot, "/backend codex"), self.context("codex"))
            self.assertIsNone(self.app.agent.live_pid(7))  # switching away closes the Claude process
            await self.app._cmd_new(make_update(self.bot, "/new"), self.context())
            await self.app._cmd_help(make_update(self.bot, "/help"), self.context())

        self.play(scenario)

        _, status, skills, schedule, backend, bad_backend, switched, fresh, help_text = self.bot.sent
        self.assertIn("Backend: claude\nModel: the claude default\nEffort: the claude default\nSession: sess-A", status)
        self.assertIn("Scheduled: 1", status)
        self.assertIn("• alfred:extensions", skills)
        self.assertIn("Call Sam", schedule)
        self.assertIn("Backend: claude", backend)
        self.assertIn("unknown backend", bad_backend)
        self.assertIn("Now using codex", switched)
        self.assertIn("Fresh conversation", fresh)
        self.assertIn("/skills", help_text)
        self.assertIsNone(self.app.agent.sessions.session(7, "claude"))

    def test_changing_the_model_in_the_settings_applies_from_the_next_message(self) -> None:
        def answering_as(model: str):
            lines = claude_lines("Hi.", "sess-A")
            lines[0]["model"] = model
            return {"fresh": lines}

        self.sandbox.fake_cli("claude", answering_as("claude-fable-5-2"))
        pids: List[Optional[int]] = []

        async def status() -> str:
            await self.app._cmd_status(make_update(self.bot, "/status"), self.context())
            return self.bot.sent[-1]

        async def scenario() -> None:
            await self.say("one")
            pids.append(self.app.agent.live_pid(7))
            self.assertIn("Model: claude-fable-5-2\n", await status())

            # The owner edits .env while the bot runs.
            self.sandbox.environ["ALFRED_CLAUDE_MODEL"] = "sonnet"
            self.sandbox.fake_cli("claude", answering_as("claude-sonnet-5-5"))
            self.assertIn("Model: sonnet\n", await status())
            await self.say("two")
            pids.append(self.app.agent.live_pid(7))
            self.assertIn("Model: sonnet (claude-sonnet-5-5)\n", await status())

            await self.say("three")  # nothing changed: same process
            pids.append(self.app.agent.live_pid(7))
            self.assertIn("Effort: the claude default\n", await status())

            self.sandbox.environ["ALFRED_CLAUDE_EFFORT"] = "max"
            self.assertIn("Effort: max\n", await status())
            await self.say("four")  # a new effort level also reopens the process
            pids.append(self.app.agent.live_pid(7))

        self.play(scenario)

        self.assertNotEqual(pids[0], pids[1])
        self.assertEqual(pids[1], pids[2])
        self.assertNotEqual(pids[2], pids[3])
        first, second, third, fourth = self.sandbox.calls("claude")
        self.assertNotIn("--effort", third["argv"])
        self.assertEqual(fourth["argv"][fourth["argv"].index("--effort") + 1], "max")
        self.assertEqual(fourth["argv"][fourth["argv"].index("--resume") + 1], "sess-A")
        self.assertNotIn("--model", first["argv"])
        self.assertEqual(second["argv"][second["argv"].index("--model") + 1], "sonnet")
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], "sess-A")  # same conversation

    def test_new_closes_the_process_and_the_next_message_starts_a_fresh_conversation(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-A")})

        async def scenario() -> None:
            await self.say("hello")
            await self.app._cmd_new(make_update(self.bot, "/new"), self.context())
            await self.say("hello again")

        self.play(scenario)

        first, second = self.sandbox.calls("claude")
        self.assertNotIn("--resume", second["argv"])
        self.assertEqual(self.bot.sent, ["Hi.", "Fresh conversation. I still have my memory and skills.", "Hi."])

    def test_a_backend_that_cannot_stay_open_runs_one_turn_per_message(self) -> None:
        self.sandbox.environ["ALFRED_BACKEND"] = "codex"
        self.sandbox.fake_cli("codex", {"fresh": [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "From codex."}},
        ]})

        async def scenario() -> None:
            await self.say("hello")
            await self.say("again")

        self.play(scenario)

        self.assertEqual(self.bot.sent, ["From codex.", "From codex."])
        self.assertEqual(len(self.sandbox.calls("codex")), 2)
        self.assertNotIn("live", self.sandbox.calls("codex")[0])

    def test_live_mode_can_be_turned_off(self) -> None:
        self.sandbox.environ["ALFRED_LIVE"] = "0"
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-A")})

        async def scenario() -> None:
            await self.say("hello")
            await self.say("again")

        self.play(scenario)

        calls = self.sandbox.calls("claude")
        self.assertEqual(self.bot.sent, ["Hi.", "Hi."])
        self.assertNotIn("--input-format", calls[0]["argv"])
        self.assertIn("--resume", calls[1]["argv"])

    def test_scheduled_message_task_and_event_delivery(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("It will rain.", "sess-A")})
        past = scheduler.utc_now() - timedelta(seconds=5)
        bot_id = self.record.bot_id
        self.app.store.add(chat_id=7, due=past, text="Call Sam", bot_id=bot_id)
        self.app.store.add(chat_id=7, due=past, text="Check the weather", kind=scheduler.KIND_TASK, username="pablo", bot_id=bot_id)

        async def scenario() -> None:
            await scheduler.run_scheduler(self.app.store, self.app._deliver, once=True)
            await self.app.agent.wait_idle(7)
            self.app.store.add(chat_id=7, due=past, text="The build finished", kind=scheduler.KIND_EVENT, bot_id=bot_id)
            await scheduler.run_scheduler(self.app.store, self.app._deliver, once=True)
            await self.app.agent.wait_idle(7)

        self.play(scenario)

        self.assertEqual(sorted(self.bot.sent), ["It will rain.", "It will rain.", "⏰ Call Sam"])
        task, event = [call["stdin"] for call in self.sandbox.calls("claude")]
        self.assertIn('source="schedule"', task)
        self.assertTrue(task.endswith("[Scheduled task] Check the weather"))
        self.assertIn('source="event"', event)
        self.assertTrue(event.endswith("[Event] The build finished"))
        self.assertEqual(self.app.store.list(7, bot_id=bot_id), [])


if __name__ == "__main__":
    unittest.main()
