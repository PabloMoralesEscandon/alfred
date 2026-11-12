"""The bots Alfred creates: registry, tokens, settings, permissions and each bot's own directory."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import unittest
from typing import Any, Dict, List

from alfred import bots, extensions, home
from alfred.agent import Agent, Chat, Listener
from alfred.bots import BotError, BotRecord, BotRegistry
from fakes import Sandbox, claude_lines

TOKEN = "5000000001:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def mode(path) -> int:
    return stat.S_IMODE(os.stat(str(path)).st_mode)


class FakeManager:
    """The control panel's `telegram.Bot`, as far as the manager calls go."""

    def __init__(self, tokens: Dict[int, str]) -> None:
        self.tokens = tokens
        self.calls: List[Any] = []

    async def do_api_request(self, endpoint: str, api_kwargs: Dict[str, Any] = None, return_type: Any = None) -> Any:
        self.calls.append((endpoint, api_kwargs))
        if endpoint == "getManagedBotToken":
            return self.tokens[api_kwargs["user_id"]]
        if endpoint == "replaceManagedBotToken":
            return "%d:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB" % api_kwargs["user_id"]
        return True


class BotsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config
        home.bootstrap(self.config, legacy_dir=None)
        self.registry = BotRegistry(self.config)
        self.work = self.sandbox.root / "work"  # a project directory, outside Alfred's home
        self.work.mkdir()

    def add_bot(self, bot_id: int = 5000000001, username: str = "research_bot", **fields: Any) -> BotRecord:
        record = BotRecord(bot_id=bot_id, username=username, name=fields.pop("name", "Research"), owner_id=99, **fields)
        self.registry.save(record)
        return record


class RegistryTests(BotsTestCase):
    def test_records_round_trip_and_are_found_by_username_name_or_id(self) -> None:
        self.add_bot(settings={"task": "Summarize papers"})
        self.add_bot(5000000002, "cooking_bot", name="Chef")
        self.assertEqual([record.username for record in self.registry.list()], ["research_bot", "cooking_bot"])
        for text in ("@research_bot", "RESEARCH_BOT", "Research", "5000000001"):
            self.assertEqual(self.registry.find(text).bot_id, 5000000001, text)
        self.assertIsNone(self.registry.find("nobody"))
        self.assertEqual(self.registry.get(5000000001).settings, {"task": "Summarize papers"})

    def test_the_token_is_private_and_belongs_to_its_bot(self) -> None:
        self.add_bot()
        self.registry.set_token(5000000001, TOKEN)
        token_file = self.registry.state_dir(5000000001) / "token"
        self.assertEqual(self.registry.token(5000000001), TOKEN)
        self.assertEqual(mode(token_file), 0o600)
        self.assertEqual(mode(token_file.parent), 0o700)
        self.assertEqual(mode(self.config.bots_state_dir), 0o700)
        self.assertEqual(mode(self.registry.state_dir(5000000001) / "bot.json"), 0o600)
        with self.assertRaises(BotError):
            self.registry.set_token(5000000001, "not a token")
        with self.assertRaises(BotError):
            self.registry.set_token(5000000001, "5000000002:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.registry.delete_token(5000000001)
        self.assertIsNone(self.registry.token(5000000001))

    def test_the_token_stays_out_of_the_homes_git_history(self) -> None:
        self.sandbox.environ["ALFRED_AUTOSAVE"] = "1"
        home.bootstrap(self.config, legacy_dir=None)
        self.add_bot()
        self.registry.set_token(5000000001, TOKEN)
        home.autosave(self.config, "test")
        tracked = home._git(self.config.home, "ls-files").stdout
        self.assertNotIn("state/", tracked)
        self.assertEqual(home._git(self.config.home, "grep", "-q", TOKEN[:20]).returncode, 1)

    def test_pending_requests_match_by_username_then_latest_and_expire(self) -> None:
        self.registry.add_pending(99, 99, "Research", "research_bot", {"task": "A"}, {"web": "on"})
        self.registry.add_pending(99, 99, "Chef", "chef_bot", {"task": "B"})
        self.assertIsNone(self.registry.take_pending(42, "research_bot"))  # someone else's
        taken = self.registry.take_pending(99, "research_bot")
        self.assertEqual((taken.name, taken.permissions), ("Research", {"web": "on"}))
        # The user renamed the bot in Telegram's window: the latest request is the one it answers.
        self.assertEqual(self.registry.take_pending(99, "renamed_bot").name, "Chef")
        self.assertIsNone(self.registry.take_pending(99, "chef_bot"))

        self.registry.add_pending(99, 99, "Old", "old_bot", {})
        data = json.loads((self.config.bots_state_dir / "pending.json").read_text())
        data[0]["created"] = time.time() - bots.PENDING_SECONDS - 1
        (self.config.bots_state_dir / "pending.json").write_text(json.dumps(data))
        self.assertIsNone(self.registry.take_pending(99, "old_bot"))

    def test_remove_deletes_state_and_directory(self) -> None:
        self.add_bot()
        self.registry.set_token(5000000001, TOKEN)
        bots.prepare_workspace(self.registry.config_for(5000000001))
        removed = self.registry.remove(5000000001)
        self.assertEqual(len(removed), 2)
        self.assertFalse(self.registry.state_dir(5000000001).exists())
        self.assertFalse(self.registry.workspace(5000000001).exists())


class ConfigTests(BotsTestCase):
    def test_a_bot_has_its_own_directory_outside_the_home(self) -> None:
        self.add_bot()
        config = self.registry.config_for(5000000001)
        workspace = self.config.bots_home / "5000000001"
        self.assertEqual(config.workspace, workspace)
        self.assertNotIn(self.config.home, workspace.parents)  # Claude Code would load Alfred's CLAUDE.md
        self.assertEqual(config.memory_dir, workspace / "memory")
        self.assertEqual(config.inbox_dir, workspace / "inbox")
        self.assertEqual(config.skills_dir, workspace / ".claude" / "skills")
        self.assertEqual(config.extensions_dir, workspace / "extensions")
        self.assertEqual(config.sessions_path, self.config.bots_state_dir / "5000000001" / "sessions.json")
        self.assertEqual(config.db_path, self.config.db_path)  # one scheduler, items name their bot
        self.assertEqual(self.config.memory_dir, self.config.home / "memory")  # the main home is unchanged

    def test_settings_override_env_and_unset_ones_fall_back(self) -> None:
        self.sandbox.environ.update(ALFRED_CLAUDE_MODEL="opus", ALFRED_CLAUDE_EFFORT="high", ALFRED_WORKDIR="/tmp")
        self.add_bot(settings={"model": "sonnet"})
        config = self.registry.config_for(5000000001)
        self.assertEqual(config.claude_model, "sonnet")
        self.assertEqual(config.claude_effort, "high")
        self.assertIsNone(config.workdir)  # a working directory never comes from Alfred's .env
        self.assertEqual(config.claude_permission_mode, "dontAsk")
        self.assertEqual(config.browser, "none")

        record = self.registry.get(5000000001)
        record.settings.update(backend="codex", model="gpt-x")
        self.registry.save(record)
        self.assertEqual((config.default_backend, config.codex_model, config.claude_model), ("codex", "gpt-x", "opus"))
        self.assertEqual(config.codex_sandbox, "read-only")

    def test_each_bot_loads_only_its_own_extensions(self) -> None:
        self.add_bot()
        self.add_bot(5000000002, "cooking_bot", name="Chef")
        research = self.registry.config_for(5000000001)
        chef = self.registry.config_for(5000000002)
        extensions.scaffold(research, "papers", "Finding and reading papers")
        self.assertEqual([path.name for path in extensions.plugin_dirs(research)], ["plugin", "papers"])
        self.assertEqual([path.name for path in extensions.plugin_dirs(chef)], ["plugin"])  # base only
        self.assertEqual([path.name for path in extensions.plugin_dirs(self.config)], ["plugin"])


    def test_a_bots_extensions_that_run_by_themselves_are_not_loaded(self) -> None:
        self.add_bot()
        research = self.registry.config_for(5000000001)
        hooked = extensions.scaffold(research, "hooked", "Runs a hook").path
        (hooked / "hooks").mkdir()
        (hooked / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "cat ~/.alfred/memory/x"}]}]}}))
        served = extensions.scaffold(research, "served", "An MCP server").path
        (served / ".mcp.json").write_text(json.dumps({"mcpServers": {"notes": {"command": "notes-server"}}}))
        extensions.scaffold(research, "plain", "Skills and commands only")
        self.assertEqual([path.name for path in extensions.plugin_dirs(research)], ["plugin", "plain"])
        self.assertEqual(extensions.held_back(research, extensions.find(research, "hooked")), ["hooks"])
        record = self.registry.get(5000000001)
        record.permissions["mcp"] = "on"
        self.registry.save(record)
        self.assertEqual([path.name for path in extensions.plugin_dirs(research)], ["plugin", "plain", "served"])
        # Alfred's main agent loads everything, as before.
        main_ext = extensions.scaffold(self.config, "mainhook", "Main agent hook").path
        (main_ext / "hooks").mkdir()
        self.assertIn("mainhook", [path.name for path in extensions.plugin_dirs(self.config)])


class SettingTests(BotsTestCase):
    def test_settings_are_checked(self) -> None:
        clean = lambda name, value: bots.clean_setting(self.config, name, value)
        self.assertEqual(clean("effort", "HIGH"), "high")
        self.assertEqual(clean("model", "default"), "")
        self.assertEqual(clean("workdir", str(self.work)), str(self.work))
        for name, value in (("effort", "huge"), ("backend", "gemini"), ("model", "a b"), ("colour", "red"),
                            ("workdir", "relative/path"), ("workdir", str(self.sandbox.root / "missing")),
                            ("workdir", str(self.config.home / "state")), ("workdir", str(self.sandbox.root)),
                            ("workdir", "/")):
            with self.assertRaises(BotError, msg=(name, value)):
                clean(name, value)

    def test_permissions_are_checked(self) -> None:
        clean = lambda name, value: bots.clean_permission(self.config, name, value)
        self.assertEqual(clean("web", "yes"), "on")
        self.assertEqual(clean("mode", "acceptedits"), "acceptEdits")
        self.assertEqual(clean("shell", "default"), "alfred")
        self.assertEqual(clean("allow", "Bash(git status); mcp__github__*"), "Bash(git status);mcp__github__*")
        for name, value in (("web", "maybe"), ("mode", "yolo"), ("shell", "some"), ("folders", "/"),
                            ("allow", "rm -rf /"), ("task", "x")):
            with self.assertRaises(BotError, msg=(name, value)):
                clean(name, value)

    def test_options_at_the_start_of_bot_new(self) -> None:
        words, rest = bots.split_options("+web -schedule model=sonnet Read the news\nand summarize it")
        self.assertEqual(words, ["+web", "-schedule", "model=sonnet"])
        self.assertEqual(rest, "Read the news\nand summarize it")
        settings, permissions, _ = bots.parse_options(self.config, words + ["+shell", "+bypass"])
        self.assertEqual(settings, {"model": "sonnet"})
        self.assertEqual(permissions, {"web": "on", "schedule": "off", "shell": "full", "mode": "bypassPermissions"})
        with self.assertRaises(BotError):
            bots.parse_options(self.config, ["+everything"])
        self.assertEqual(bots.split_options("-5 degrees outside"), ([], "-5 degrees outside"))

    def test_usernames_extend_the_panels(self) -> None:
        suggest = bots.suggest_username
        self.assertEqual(suggest("Gestor calendarios", "manolito_bot"), "manolito_gestor_calendarios_bot")
        self.assertEqual(suggest("Research", "AlfredPanelBot"), "alfredpanel_research_bot")
        self.assertEqual(suggest("Research", "@Manolito_Bot"), "manolito_research_bot")
        self.assertEqual(bots.username_prefix("manolitobot"), "manolito")
        # Without a panel username (the CLI before it knows it): just the slug.
        self.assertEqual(suggest("Research"), "research_bot")
        self.assertEqual(suggest("42"), "a_42_bot")

    def test_usernames_from_odd_names(self) -> None:
        suggest = bots.suggest_username
        self.assertEqual(suggest("Ñandú Café", "manolito_bot"), "manolito_nandu_cafe_bot")  # accents
        self.assertEqual(suggest("  My   NEWS  Bot ", "manolito_bot"), "manolito_my_news_bot")  # spaces, capitals, its own "bot"
        self.assertEqual(suggest("📅 Agenda ✨", "manolito_bot"), "manolito_agenda_bot")  # emojis
        self.assertEqual(suggest("📅", "manolito_bot", "Organiza mi agenda"), "manolito_organiza_mi_agenda_bot")  # from the task
        self.assertEqual(suggest("📅", "manolito_bot"), "manolito_agent_bot")  # nothing usable at all
        self.assertEqual(suggest("a--b__c", "manolito_bot"), "manolito_a_b_c_bot")
        for name in ("Ñandú Café", "📅", "a" * 50, "x y z " * 10, "日本語のボット"):
            username = suggest(name, "manolito_bot")
            self.assertRegex(username, r"^[a-z][a-z0-9_]{3,31}$", name)
            self.assertNotIn("__", username)
            self.assertTrue(username.endswith("_bot"))

    def test_long_usernames_are_cut_in_the_slug_only(self) -> None:
        suggest = bots.suggest_username
        username = suggest("Gestor de Calendarios de la Oficina", "manolito_bot")
        self.assertEqual(username, "manolito_gestor_de_calendari_bot")
        self.assertEqual(len(username), 32)
        self.assertTrue(username.startswith("manolito_"))
        # A cut that lands on an underscore leaves none hanging.
        self.assertEqual(suggest("abcdefghijklmnopqr st", "manolito_bot"), "manolito_abcdefghijklmnopqr_bot")
        # A panel name so long that prefix_slug_bot can never fit: the slug goes, the prefix stays as far as it can.
        self.assertLessEqual(len(suggest("Research", "b" * 29 + "bot")), 32)

    def test_usernames_that_are_taken_get_a_number(self) -> None:
        suggest = bots.suggest_username
        self.assertEqual(suggest("Research", "manolito_bot", taken=["manolito_research_bot"]), "manolito_research_2_bot")
        self.assertEqual(suggest("Research", "manolito_bot", taken=["Manolito_Research_Bot", "manolito_research_2_bot"]),
                         "manolito_research_3_bot")
        long_name = "Gestor de Calendarios de la Oficina"
        first = suggest(long_name, "manolito_bot")
        second = suggest(long_name, "manolito_bot", taken=[first])
        self.assertEqual(second, "manolito_gestor_de_calenda_2_bot")
        self.assertEqual(len(second), 32)
        # The panel's own username is never suggested.
        self.assertEqual(suggest("", "manolito_bot", "", taken=[]), "manolito_agent_bot")
        self.assertEqual(suggest("Agent", "manolito_agent_bot"), "manolito_agent_agent_bot")

    def test_known_usernames_include_bots_and_pending_requests(self) -> None:
        self.add_bot(username="manolito_research_bot")
        self.registry.add_pending(99, 99, "Chef", "manolito_chef_bot", {})
        taken = self.registry.known_usernames()
        self.assertEqual(sorted(taken), ["manolito_chef_bot", "manolito_research_bot"])
        self.assertEqual(bots.suggest_username("Chef", "manolito_bot", taken=taken), "manolito_chef_2_bot")

    def test_creation_links(self) -> None:
        self.assertEqual(
            bots.creation_link("@AlfredPanelBot", "research_bot", "Research Bot"),
            "https://t.me/newbot/AlfredPanelBot/research_bot?name=Research+Bot",
        )


class PermissionTests(BotsTestCase):
    def settings_for(self, **permissions: str) -> Dict[str, Any]:
        record = self.add_bot(permissions=permissions, settings={"workdir": str(self.work)})
        return bots.claude_settings(self.registry.config_for(record.bot_id), self.registry.get(record.bot_id))

    def test_a_new_bot_starts_with_the_least(self) -> None:
        rules = self.settings_for()["permissions"]
        workspace = "//" + str(self.config.bots_home / "5000000001").lstrip("/")
        self.assertEqual(rules["defaultMode"], "dontAsk")
        self.assertIn("Bash(alfred *)", rules["allow"])
        self.assertNotIn("Bash", rules["allow"])
        self.assertIn("Edit(%s/**)" % workspace, rules["allow"])
        for denied in ("WebFetch", "WebSearch", "mcp__*", "Bash(orca *)", "Bash(alfred bots *)", "Bash(alfred run *)",
                       "Read(//%s/**)" % str(self.config.home).lstrip("/"), "Edit(%s/CLAUDE.md)" % workspace):
            self.assertIn(denied, rules["deny"], denied)
        self.assertNotIn("Bash(alfred schedule *)", rules["deny"])  # reminders are on by default
        self.assertTrue(rules["blockReadsOutsideWorkingDirectories"])
        self.assertTrue(rules["blockWritesOutsideWorkingDirectories"])
        self.assertTrue(rules["denyInsteadOfPrompting"])
        self.assertEqual(rules["additionalDirectories"], [str(self.work)])
        self.assertNotIn("Edit(//%s/**)" % str(self.work).lstrip("/"), rules["allow"])  # read only

    def test_granted_permissions_become_allow_rules_and_a_sandbox(self) -> None:
        settings = self.settings_for(web="on", shell="full", edit="on", browser="on", schedule="off", bots="on",
                                     allow="mcp__github__*", deny="Bash(git push *)")
        rules = settings["permissions"]
        self.assertIn("Bash", rules["allow"])
        self.assertIn("WebFetch(domain:*)", rules["allow"])
        self.assertIn("Edit(//%s/**)" % str(self.work).lstrip("/"), rules["allow"])
        self.assertIn("mcp__claude-in-chrome__*", rules["allow"])
        self.assertIn("mcp__github__*", rules["allow"])
        self.assertIn("Bash(git push *)", rules["deny"])
        self.assertIn("Bash(alfred schedule *)", rules["deny"])
        self.assertNotIn("mcp__*", rules["deny"])  # the browser is an MCP server
        sandbox = settings["sandbox"]
        self.assertEqual((sandbox["enabled"], sandbox["allowUnsandboxedCommands"], sandbox["failIfUnavailable"]), (True, False, True))
        self.assertIn(str(self.config.home), sandbox["filesystem"]["denyRead"])
        self.assertIn("alfred bots *", sandbox["excludedCommands"])

        no_sandbox = self.settings_for(shell="full", sandbox="off")
        self.assertNotIn("sandbox", no_sandbox)

    def test_summary_for_the_bot_and_for_the_owner(self) -> None:
        record = self.add_bot(permissions={"web": "on"})
        to_bot = bots.permission_summary(record)
        self.assertIn("You can", to_bot)
        self.assertIn("search and fetch web pages", to_bot.split("You cannot")[0])
        self.assertIn("use the browser", to_bot.split("You cannot")[1])
        self.assertIn("control panel", to_bot)
        self.assertTrue(bots.permission_summary(record, for_owner=True).startswith("It can"))


class BlacklistTests(BotsTestCase):
    """Other agents' memory and tools are closed at every level below cross-agent access, whatever the switches."""

    RESEARCH, CHEF = 5000000001, 5000000002

    def setUp(self) -> None:
        super().setUp()
        self.add_bot(self.RESEARCH, "research_bot")
        self.add_bot(self.CHEF, "chef_bot", name="Chef")
        for bot_id in (self.RESEARCH, self.CHEF):
            config = self.registry.config_for(bot_id)
            bots.prepare_workspace(config)
            (config.extensions_dir / "tools" / "bin").mkdir(parents=True)
            (config.skills_dir / "cook").mkdir(parents=True)
        (self.config.home / ".claude" / "skills" / "mail").mkdir(parents=True, exist_ok=True)
        self.chef = self.config.bots_home / str(self.CHEF)
        self.chef_project = bots.claude_project_dir(self.config, self.chef)
        self.alfred_project = bots.claude_project_dir(self.config, self.config.home)

    def settings(self, permissions: Dict[str, str], cross: bool = False) -> Dict[str, Any]:
        record = self.registry.get(self.RESEARCH)
        record.permissions = dict(permissions)
        record.cross_agent = cross
        self.registry.save(record)
        return bots.claude_settings(self.registry.config_for(self.RESEARCH), self.registry.get(self.RESEARCH))

    @staticmethod
    def rule(tool: str, path: Any, suffix: str = "/**") -> str:
        return "%s(//%s%s)" % (tool, str(path).lstrip("/"), suffix)

    def levels(self) -> Dict[str, Dict[str, str]]:
        from alfred import menus

        levels = {name: dict(preset) for name, preset in menus.PRESETS.items()}  # Minimal, Standard, Broad
        levels["full access"] = {name: "on" for name in bots.SWITCHES if name != "sandbox"}
        levels["full access"].update(shell="full", sandbox="off", mode="bypassPermissions")
        levels["full access, sandboxed"] = dict(levels["full access"], sandbox="on")
        levels["auto"] = {"mode": "auto"}
        levels["bypass, shell alfred"] = {"mode": "bypassPermissions"}
        # Extra allow rules that name the other agents' files: deny rules win over them.
        levels["allow rules"] = {"allow": "Read(%s/**);Bash" % self.rule("", self.chef, "")[1:-1], "edit": "on"}
        # Every combination of the switches, the two shells and every mode.
        for index, mode in enumerate(bots.PERMISSION_MODES):
            for shell in ("alfred", "full"):
                switches = {name: ("on" if (bit + index) % 2 else "off") for bit, name in enumerate(bots.SWITCHES)}
                levels["mix %s %s" % (mode, shell)] = dict(switches, mode=mode, shell=shell)
        return levels

    def assert_closed(self, settings: Dict[str, Any], level: str) -> None:
        rules = settings["permissions"]
        for path in (self.chef, self.chef_project, self.config.home, self.alfred_project):
            for tool in ("Read", "Edit"):  # Read deny rules also hold for Grep, Glob, cat, head...
                self.assertIn(self.rule(tool, path), rules["deny"], "%s: %s %s" % (level, tool, path))
        for path in (self.chef / "memory", self.chef / "extensions", self.chef / ".claude",
                     self.config.home / "memory", self.config.home / "extensions", self.config.home / ".claude"):
            self.assertIn("Bash(*%s/*)" % path, rules["deny"], "%s: shell rule for %s" % (level, path))
        self.assertIn("Bash(*../%d/memory/*)" % self.CHEF, rules["deny"], level)
        for directory in rules["additionalDirectories"]:
            self.assertNotIn(str(self.config.bots_home / str(self.CHEF)), directory, level)
            self.assertFalse(directory.startswith(str(self.config.home)), level)
        self.assertTrue(rules["blockReadsOutsideWorkingDirectories"], level)
        self.assertTrue(settings["disableAllHooks"], level)
        if "sandbox" in settings:
            filesystem = settings["sandbox"]["filesystem"]
            for path in (self.chef, self.chef_project, self.config.home, self.alfred_project):
                self.assertIn(str(path), filesystem["denyRead"], level)
                self.assertIn(str(path), filesystem["denyWrite"], level)
            self.assertIn(str(self.config.bots_home), filesystem["denyRead"], level)  # bots created later too
            self.assertEqual(filesystem["allowRead"], [str(self.config.bots_home / str(self.RESEARCH))], level)
            self.assertFalse(settings["sandbox"]["autoAllowBashIfSandboxed"], level)

    def test_the_blacklist_is_there_at_every_level(self) -> None:
        for level, permissions in self.levels().items():
            self.assert_closed(self.settings(permissions), level)

    def test_an_open_shell_runs_in_the_sandbox_unless_it_is_switched_off(self) -> None:
        for permissions in ({"shell": "full"}, {"mode": "bypassPermissions"}, {"mode": "auto"}):
            self.assertIn("sandbox", self.settings(permissions), permissions)
        self.assertNotIn("sandbox", self.settings({}))  # only `alfred`: nothing to sandbox
        self.assertNotIn("sandbox", self.settings({"shell": "full", "sandbox": "off"}))

    def test_the_switches_cannot_take_it_away(self) -> None:
        for word in bots.CROSS_AGENT_WORDS:
            with self.assertRaises(BotError):
                bots.clean_permission(self.config, word, "on")
            with self.assertRaises(BotError):
                bots.parse_options(self.config, ["+%s" % word])
            with self.assertRaises(BotError):
                bots.parse_options(self.config, ["%s=on" % word])
        self.assertEqual(bots.split_options("+cross Do it")[0], ["+cross"])  # an option, refused: never the task
        for folder in (self.chef, self.chef / "memory", self.config.home / "memory"):
            with self.assertRaises(BotError):
                bots.clean_permission(self.config, "folders", str(folder))
            with self.assertRaises(BotError):
                bots.clean_setting(self.config, "workdir", str(folder))
        # Every permission at every value it takes leaves cross-agent access off.
        for name, _, _ in bots.PERMISSIONS:
            for value in ("on", "off", "full", "alfred", "bypassPermissions", "default"):
                try:
                    cleaned = bots.clean_permission(self.config, name, value)
                except BotError:
                    continue
                record = self.registry.get(self.RESEARCH)
                record.permissions[name] = cleaned
                self.registry.save(record)
                self.assertFalse(self.registry.get(self.RESEARCH).cross_agent, (name, value))
        # Writing "cross_agent" into the permissions does nothing; only a real true in its own field counts.
        record = self.registry.get(self.RESEARCH)
        record.permissions["cross_agent"] = "on"
        self.registry.save(record)
        self.assertFalse(self.registry.get(self.RESEARCH).cross_agent)
        path = self.registry.state_dir(self.RESEARCH) / "bot.json"
        data = json.loads(path.read_text())
        data["cross_agent"] = "true"
        path.write_text(json.dumps(data))
        self.assertFalse(self.registry.get(self.RESEARCH).cross_agent)

    def test_cross_agent_access_opens_memory_and_tools_to_read_and_run(self) -> None:
        for permissions in ({}, {"shell": "full"}, {"shell": "full", "mode": "bypassPermissions"}):
            settings = self.settings(permissions, cross=True)
            rules = settings["permissions"]
            for path in (self.chef, self.config.home):
                self.assertNotIn(self.rule("Read", path), rules["deny"])
                self.assertIn(self.rule("Edit", path), rules["deny"])  # read and run, never change
            self.assertNotIn("Bash(*%s/*)" % (self.chef / "memory"), rules["deny"])
            for shared in (self.chef / "memory", self.chef / "extensions", self.chef / ".claude" / "skills",
                           self.config.home / "memory", self.config.home / "extensions"):
                self.assertIn(str(shared), rules["additionalDirectories"])
            self.assertIn("Bash(%s/*/bin/*)" % (self.chef / "extensions"), rules["allow"])
            # Still closed: tokens and secrets, their conversations and inboxes.
            for closed in (self.config.home / "state", self.chef / "inbox", self.chef / ".git"):
                self.assertIn(self.rule("Read", closed), rules["deny"])
            self.assertIn(self.rule("Read", self.config.home / ".env", ""), rules["deny"])
            self.assertIn(self.rule("Read", self.chef_project, "/*.jsonl"), rules["deny"])
            self.assertIn("Bash(*%s/*)" % (self.config.home / "state"), rules["deny"])
            if "sandbox" in settings:
                filesystem = settings["sandbox"]["filesystem"]
                self.assertIn(str(self.chef / "memory"), filesystem["allowRead"])
                self.assertIn(str(self.chef), filesystem["denyRead"])  # the rest of it stays closed
                self.assertIn(str(self.chef), filesystem["denyWrite"])
                self.assertIn(str(self.config.home / ".env"), filesystem["denyRead"])
                self.assertNotIn(str(self.config.home / "state"), filesystem["allowRead"])
        # Only the bot that has it: the other one stays closed.
        chef = bots.claude_settings(self.registry.config_for(self.CHEF), self.registry.get(self.CHEF))
        self.assertIn(self.rule("Read", self.config.bots_home / str(self.RESEARCH)), chef["permissions"]["deny"])

    def test_the_manual_and_the_summaries_say_which_it_is(self) -> None:
        self.settings({})
        config = self.registry.config_for(self.RESEARCH)
        record = self.registry.get(self.RESEARCH)
        self.assertIn("closed to you", bots.render_manual(config, record))
        self.assertIn("cannot read the memory of other agents", bots.permission_summary(record, for_owner=True))
        self.assertIn("🔒", bots.describe(record))
        record.cross_agent, record.cross_agent_since = True, "2026-11-12T10:00:00Z"
        manual = bots.render_manual(config, record)
        self.assertIn("cross-agent access", manual)
        self.assertIn(str(self.chef / "memory"), manual)
        self.assertIn(str(self.config.home / "memory"), manual)
        self.assertIn("CROSS-AGENT ACCESS ON since 2026-11-12T10:00:00Z", bots.describe(record))

    def test_the_audit_log_is_private_and_appends(self) -> None:
        record = self.registry.get(self.RESEARCH)
        bots.audit(self.config, "cross_agent_granted", record, 99, "command")
        bots.audit(self.config, "cross_agent_revoked", record, 99, "menu")
        path = self.config.state_dir / bots.AUDIT_FILE
        self.assertEqual(mode(path), 0o600)
        entries = bots.read_audit(self.config)
        self.assertEqual([(entry["event"], entry["via"], entry["bot_id"], entry["by"]) for entry in entries],
                         [("cross_agent_granted", "command", self.RESEARCH, 99), ("cross_agent_revoked", "menu", self.RESEARCH, 99)])


class WorkspaceTests(BotsTestCase):
    def test_prepare_writes_manual_settings_and_memory(self) -> None:
        self.sandbox.environ["ALFRED_AUTOSAVE"] = "1"
        self.add_bot(settings={"task": "Summarize new papers on protein folding.", "workdir": str(self.work)})
        config = self.registry.config_for(5000000001)
        folder = bots.prepare_workspace(config)
        manual = (folder / "CLAUDE.md").read_text()
        self.assertIn("You are Research (@research_bot)", manual)
        self.assertIn("Summarize new papers on protein folding.", manual)
        self.assertIn("Your working directory for this task is `%s`" % self.work, manual)
        self.assertIn("You cannot", manual)
        self.assertTrue(manual.rstrip().endswith("@memory/MEMORY.md"))
        self.assertNotIn("@memory", (folder / "AGENTS.md").read_text())
        self.assertTrue((folder / "memory" / "MEMORY.md").is_file())
        self.assertTrue((folder / ".git").is_dir())
        settings_file = self.registry.settings_file(5000000001)
        self.assertEqual(mode(settings_file), 0o600)
        self.assertEqual(json.loads(settings_file.read_text())["permissions"]["defaultMode"], "dontAsk")

        # A changed task rewrites the manual; nothing else the bot wrote is touched.
        (folder / "memory" / "notes.md").write_text("mine")
        record = self.registry.get(5000000001)
        record.settings["task"] = "Track arXiv daily."
        self.registry.save(record)
        bots.prepare_workspace(config)
        self.assertIn("Track arXiv daily.", (folder / "CLAUDE.md").read_text())
        self.assertEqual((folder / "memory" / "notes.md").read_text(), "mine")

    def test_files_a_bot_may_send(self) -> None:
        record = self.add_bot(settings={"workdir": str(self.work)})
        config = self.registry.config_for(record.bot_id)
        bots.prepare_workspace(config)
        self.assertTrue(bots.path_is_allowed(config, record, config.workspace / "outbox" / "a.png"))
        self.assertTrue(bots.path_is_allowed(config, record, self.work / "report.pdf"))
        self.assertFalse(bots.path_is_allowed(config, record, self.config.home / ".env"))
        self.assertFalse(bots.path_is_allowed(config, record, config.workspace / ".." / "5000000002" / "x"))


class AgentIsolationTests(BotsTestCase):
    def test_a_bots_agent_runs_in_its_own_directory_with_its_permissions(self) -> None:
        self.sandbox.environ["TELEGRAM_BOT_TOKEN"] = "123:secret"
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:secret"
        self.addCleanup(os.environ.pop, "TELEGRAM_BOT_TOKEN", None)
        lines = claude_lines("Hi.", "sess-R")
        probe = 'pwd > %s; env > %s' % (self.sandbox.root / "cwd.txt", self.sandbox.root / "env.txt")
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_run": probe}] + lines[1:]})
        self.add_bot(settings={"workdir": str(self.work), "model": "sonnet"}, permissions={"web": "on"})
        agent = Agent(self.registry.config_for(5000000001))
        agent.bot_username = "research_bot"

        async def scenario() -> None:
            await agent.submit(Chat(99), "hello", user_id=99)
            await agent.wait_idle(99)
            await agent.shutdown()

        asyncio.run(scenario())
        call = self.sandbox.calls("claude")[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.work))
        self.assertEqual(argv[argv.index("--settings") + 1], str(self.registry.settings_file(5000000001)))
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        self.assertIn('bot="@research_bot"', call["stdin"])
        workspace = self.registry.workspace(5000000001)
        self.assertEqual(os.path.realpath((self.sandbox.root / "cwd.txt").read_text().strip()), os.path.realpath(str(workspace)))
        env = dict(line.split("=", 1) for line in (self.sandbox.root / "env.txt").read_text().splitlines() if "=" in line)
        self.assertEqual((env["ALFRED_BOT_ID"], env["ALFRED_CHAT_ID"], env["ALFRED_WORKDIR"]), ("5000000001", "99", str(self.work)))
        self.assertEqual(env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertEqual(self.registry.config_for(5000000001).sessions_path.exists(), True)

    def test_two_bots_with_the_same_chat_never_share_a_session_or_memory(self) -> None:
        write = "mkdir -p memory && echo remembered > memory/x.md"
        lines = claude_lines("Noted.", "sess-A")
        self.sandbox.fake_cli("claude", {"fresh": lines[:1] + [{"_run": write}] + lines[1:]})
        self.add_bot()
        self.add_bot(5000000002, "cooking_bot", name="Chef")
        results: List[Any] = []

        class Recorder(Listener):
            async def result(self, chat_id: int, result: Any) -> None:
                results.append(result)

        research = Agent(self.registry.config_for(5000000001), Recorder())
        chef = Agent(self.registry.config_for(5000000002), Recorder())

        async def scenario() -> None:
            await research.submit(99, "remember x")
            await research.wait_idle(99)
            self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hello.", "sess-B")})
            await chef.submit(99, "hi")
            await chef.wait_idle(99)
            await research.shutdown()
            await chef.shutdown()

        asyncio.run(scenario())
        self.assertEqual([result.memory_changes for result in results], [["saved x"], []])
        self.assertEqual(research.sessions.session(99, "claude"), "sess-A")
        self.assertEqual(chef.sessions.session(99, "claude"), "sess-B")
        self.assertNotIn("--resume", self.sandbox.calls("claude")[1]["argv"])  # Chef did not resume Research's
        self.assertTrue((self.registry.workspace(5000000001) / "memory" / "x.md").exists())
        self.assertFalse((self.registry.workspace(5000000002) / "memory" / "x.md").exists())
        self.assertFalse((self.config.memory_dir / "x.md").exists())

    def test_a_topic_is_its_own_conversation(self) -> None:
        self.sandbox.fake_cli("claude", {"fresh": claude_lines("Hi.", "sess-T")})
        self.add_bot()
        agent = Agent(self.registry.config_for(5000000001))
        asyncio.run(agent.ask(Chat(99, 3), "in a topic"))
        self.assertEqual(agent.sessions.session(Chat(99, 3), "claude"), "sess-T")
        self.assertIsNone(agent.sessions.session(99, "claude"))
        self.assertIn('topic_id="3"', self.sandbox.calls("claude")[0]["stdin"])
        self.assertEqual(Chat(99), 99)
        self.assertNotEqual(Chat(99, 3), 99)
        self.assertEqual({Chat(99): "a"}.get(99), "a")


class TelegramShapesTests(unittest.TestCase):
    def test_managed_bot_update_from_any_library_version(self) -> None:
        payload = {"user": {"id": 99, "is_bot": False, "first_name": "P"},
                   "bot": {"id": 5000000001, "is_bot": True, "first_name": "Research", "username": "research_bot"}}
        try:
            from telegram import Update
        except ImportError:  # pragma: no cover
            update = type("U", (), {"api_kwargs": {"managed_bot": payload}})()
        else:
            update = Update.de_json({"update_id": 1, "managed_bot": payload}, None)
        creator, bot = bots.managed_bot_payload(update)
        self.assertEqual((creator["id"], bot["username"]), (99, "research_bot"))
        self.assertIsNone(bots.managed_bot_payload(type("U", (), {"api_kwargs": {}})()))

        message = type("M", (), {"api_kwargs": {"managed_bot_created": {"bot": {"id": 7, "is_bot": True}}}})()
        self.assertEqual(bots.managed_bot_created(message)["id"], 7)
        self.assertIsNone(bots.managed_bot_created(type("M", (), {"api_kwargs": {}})()))
        self.assertTrue(bots.can_manage_bots(type("Me", (), {"api_kwargs": {"can_manage_bots": True}})()))
        self.assertIsNone(bots.can_manage_bots(type("Me", (), {"api_kwargs": {}})()))

    def test_manager_calls(self) -> None:
        manager = FakeManager({5000000001: TOKEN})

        async def calls() -> str:
            token = await bots.fetch_token(manager, 5000000001)
            await bots.restrict_access(manager, 5000000001)
            await bots.revoke_token(manager, 5000000001)
            return token

        self.assertEqual(asyncio.run(calls()), TOKEN)
        self.assertEqual([call[0] for call in manager.calls],
                         ["getManagedBotToken", "setManagedBotAccessSettings", "replaceManagedBotToken"])
        self.assertEqual(manager.calls[1][1], {"user_id": 5000000001, "is_access_restricted": True})
        with self.assertRaises(BotError):
            asyncio.run(bots.fetch_token(FakeManager({5000000001: "nope"}), 5000000001))


if __name__ == "__main__":
    unittest.main()
