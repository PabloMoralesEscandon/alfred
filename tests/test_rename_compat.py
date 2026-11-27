"""What is left of the old name on purpose, until an install is migrated (docs/RENAME_PLAN.md).

This file names the old `alfred` command and `ALFRED_*` settings on purpose: it is listed in `.rename-allow`.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import unittest
from typing import Any, Dict, List, Set
from unittest import mock

import test_credentials
from consiglieri import bots, extensions, home, keepass, presets, service
from consiglieri import config as config_module
from consiglieri.agent import Agent
from consiglieri.config import (COMMAND, COMMAND_NAMES, LEGACY_COMMAND, Config, env_get, in_bot_turn, legacy_name,
                                with_legacy_names)

from fakes import Sandbox


class LegacySettingsTests(unittest.TestCase):
    """Phase 3: every CONSIGLIERI_X also reads ALFRED_X from the same place; the new name wins."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config
        self.env_file = self.sandbox.root / "repo.env"

    def test_legacy_name(self) -> None:
        self.assertEqual(legacy_name("CONSIGLIERI_TIMEOUT"), "ALFRED_TIMEOUT")
        self.assertEqual(legacy_name("TELEGRAM_BOT_TOKEN"), "")

    def test_only_the_old_name(self) -> None:
        self.env_file.write_text("ALFRED_TIMEOUT=30\nALFRED_ALLOWED_USERS=pablo\n", encoding="utf-8")
        self.assertEqual(self.config.timeout_seconds, 30.0)
        self.assertTrue(self.config.is_allowed(1, "pablo"))
        self.sandbox.environ["ALFRED_BACKEND"] = "codex"
        self.assertEqual(self.config.default_backend, "codex")

    def test_only_the_new_name(self) -> None:
        self.env_file.write_text("CONSIGLIERI_TIMEOUT=31\n", encoding="utf-8")
        self.assertEqual(self.config.timeout_seconds, 31.0)

    def test_both_the_new_one_wins_and_it_is_logged_once(self) -> None:
        config_module._warned.discard("CONSIGLIERI_TIMEOUT")
        self.env_file.write_text("ALFRED_TIMEOUT=30\nCONSIGLIERI_TIMEOUT=31\n", encoding="utf-8")
        with self.assertLogs("consiglieri.config", "WARNING") as logs:
            self.assertEqual(self.config.timeout_seconds, 31.0)
            self.assertEqual(self.config.timeout_seconds, 31.0)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("ALFRED_TIMEOUT", logs.output[0])

    def test_each_place_in_turn(self) -> None:
        # An old name in the environment beats a new one in a file: the places keep their order.
        self.env_file.write_text("CONSIGLIERI_TIMEOUT=31\n", encoding="utf-8")
        self.sandbox.environ["ALFRED_TIMEOUT"] = "40"
        self.assertEqual(self.config.timeout_seconds, 40.0)
        # A bot's own setting under the old name beats the environment.
        self.config.overrides = lambda: {"ALFRED_TIMEOUT": "50"}
        self.assertEqual(self.config.timeout_seconds, 50.0)

    def test_load_takes_the_old_home_and_env_file(self) -> None:
        home = self.sandbox.root / "old-home"
        env_file = self.sandbox.root / "other.env"
        loaded = Config.load({"ALFRED_HOME": str(home), "ALFRED_ENV_FILE": str(env_file)})
        self.assertEqual(loaded.home, home.resolve())
        self.assertEqual(loaded.env_files, [home.resolve() / ".env", env_file])
        newer = Config.load({"ALFRED_HOME": str(home), "CONSIGLIERI_HOME": str(self.sandbox.root / "new")})
        self.assertEqual(newer.home, (self.sandbox.root / "new").resolve())

    def test_env_get(self) -> None:
        self.assertEqual(env_get("CONSIGLIERI_CHAT_ID", environ={"ALFRED_CHAT_ID": "7"}), "7")
        self.assertEqual(env_get("CONSIGLIERI_CHAT_ID", environ={"ALFRED_CHAT_ID": "7", "CONSIGLIERI_CHAT_ID": "8"}), "8")
        self.assertEqual(env_get("CONSIGLIERI_CHAT_ID", "x", environ={}), "x")

    def test_a_bot_turn_is_seen_under_either_name(self) -> None:
        self.assertFalse(in_bot_turn({}))
        self.assertFalse(in_bot_turn({"CONSIGLIERI_BOT_ID": "0"}))
        self.assertTrue(in_bot_turn({"CONSIGLIERI_BOT_ID": "5"}))
        self.assertTrue(in_bot_turn({"ALFRED_BOT_ID": "5"}))
        self.assertTrue(in_bot_turn({"ALFRED_BOT_KEY": "abc"}))
        # An empty or zero new name never hides the old one.
        self.assertTrue(in_bot_turn({"CONSIGLIERI_BOT_ID": "0", "ALFRED_BOT_ID": "5"}))
        self.assertTrue(in_bot_turn({"CONSIGLIERI_BOT_ID": "", "ALFRED_BOT_KEY": "abc"}))

    def test_the_users_commands_refuse_a_bot_with_only_the_old_names(self) -> None:
        from consiglieri import creds_cli

        with mock.patch.dict(os.environ, {"ALFRED_BOT_ID": "5"}):
            with self.assertRaises(creds_cli.CredsError):
                creds_cli.owner_only("setup")

    def test_the_agent_gets_both_names(self) -> None:
        _, config = self.sandbox.add_bot()
        with mock.patch.dict(os.environ, {"ALFRED_HOME": "/somewhere/stale", "ALFRED_BOT_ID": "1"}):
            env = Agent(config)._environment(7, 99, "pablo")
        for name in ("HOME", "BOT_ID", "BOT_KEY", "CHAT_ID", "WORKSPACE", "USER_ID", "USERNAME"):
            self.assertEqual(env["ALFRED_" + name], env["CONSIGLIERI_" + name], name)
        self.assertEqual(env["CONSIGLIERI_HOME"], str(self.sandbox.home))
        self.assertEqual(env["CONSIGLIERI_BOT_ID"], "424242")

    def test_with_legacy_names(self) -> None:
        self.assertEqual(with_legacy_names({"CONSIGLIERI_X": "1", "ALFRED_X": "old", "PATH": "p"}),
                         {"CONSIGLIERI_X": "1", "ALFRED_X": "1", "PATH": "p"})

    def test_the_soul_writer_drops_both_prefixes(self) -> None:
        from consiglieri.soul_writer import SoulWriter

        self.sandbox.environ.update({"ALFRED_SECRET_THING": "x", "CONSIGLIERI_OTHER": "y"})
        env = SoulWriter(self.config).environment()
        self.assertFalse([key for key in env if key.startswith(("ALFRED_", "CONSIGLIERI_"))])


COMMAND_WORD = re.compile(r"(^|[(*\s])(consiglieri|alfred)(?= |\)|$)")


def command_rules(settings: Dict[str, Any]) -> Set[str]:
    """Every rule or sandbox exclusion that names the command, under either name."""
    permissions = settings["permissions"]
    found = list(permissions["allow"]) + list(permissions["deny"])
    found += settings.get("sandbox", {}).get("excludedCommands", [])
    return {rule for rule in found if COMMAND_WORD.search(rule)}


def swap(rule: str, old: str, new: str) -> str:
    return COMMAND_WORD.sub(lambda match: match.group(1) + (new if match.group(2) == old else match.group(2)), rule)


class BothCommandNamesTests(unittest.TestCase):
    """Finding 5: while the old command exists, every rule that names the command holds for both names."""

    USER_ONLY = ("run", "run --dry", "service", "service status", "service install", "init", "init --x", "chat",
                 "chat --bot x", "browser-setup", "browser-setup --x", "migrate", "migrate --dry-run",
                 "browser install", "creds setup", "creds add x", "creds remove x", "creds grant x y",
                 "creds revoke x y", "creds start", "creds stop")

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def all_permission_sets(self) -> List[Dict[str, str]]:
        found = [presets.resolve(key) for key in presets.choices()]
        for name in bots.SWITCHES:  # each switch both ways, from the least and from the most
            for base in ("basic", "full"):
                for value in ("on", "off"):
                    found.append(dict(presets.resolve(base), **{name: value}))
        for shell in (COMMAND, LEGACY_COMMAND, "full"):
            found.append(dict(presets.resolve("power"), shell=shell))
        return found

    def settings_for(self, index: int, permissions: Dict[str, str]) -> Dict[str, Any]:
        record, config = self.sandbox.add_bot(bot_id=5000000000 + index, username="b%d_bot" % index,
                                              permissions=permissions)
        return bots.claude_settings(config, bots.BotRegistry(self.sandbox.config).get(record.bot_id))

    def test_the_two_names(self) -> None:
        self.assertEqual(COMMAND_NAMES, ("consiglieri", "alfred"))

    def test_every_rule_is_written_for_both_names(self) -> None:
        for index, permissions in enumerate(self.all_permission_sets()):
            rules = command_rules(self.settings_for(index, permissions))
            with self.subTest(permissions=permissions):
                self.assertTrue(rules)
                for rule in rules:
                    self.assertIn(swap(rule, "consiglieri", "alfred"), rules)
                    self.assertIn(swap(rule, "alfred", "consiglieri"), rules)

    def test_the_users_subcommands_are_denied_under_both_names_at_every_level(self) -> None:
        for index, permissions in enumerate(self.all_permission_sets()):
            deny = self.settings_for(index, permissions)["permissions"]["deny"]
            for name in COMMAND_NAMES:
                for subcommand in self.USER_ONLY:
                    command = "%s %s" % (name, subcommand)
                    with self.subTest(permissions=permissions, command=command):
                        self.assertTrue(any(matches(rule, command) for rule in deny), command)

    def test_closed_switches_close_both_names(self) -> None:
        settings = self.settings_for(0, dict(presets.resolve("basic"), schedule="off", usage="off", creds="off",
                                              bots="off", admin="off", browser="off"))
        deny = settings["permissions"]["deny"]
        for name in COMMAND_NAMES:
            for command in ("schedule add x", "usage check", "usage-alert add x", "creds list", "bots list",
                            "bots spec", "bots set x", "browser open x"):
                with self.subTest(command="%s %s" % (name, command)):
                    self.assertTrue(any(matches(rule, "%s %s" % (name, command)) for rule in deny))

    def test_the_only_shell_allows_both_names_and_nothing_else(self) -> None:
        allow = self.settings_for(0, presets.resolve("basic"))["permissions"]["allow"]
        bash = [rule for rule in allow if rule.startswith("Bash")]
        self.assertEqual(sorted(bash), ["Bash(alfred *)", "Bash(consiglieri *)"])

    def test_sandbox_exclusions_hold_for_both_names(self) -> None:
        excluded = self.settings_for(0, dict(presets.resolve("power"), usage="on", browser="on", creds="on",
                                              bots="on"))["sandbox"]["excludedCommands"]
        for name in COMMAND_NAMES:
            for command in ("send *", "schedule *", "event *", "skills", "bots *", "usage *", "browser *",
                            "creds list"):
                self.assertIn("%s %s" % (name, command), excluded)
        self.assertFalse([command for command in excluded if command.split(" ", 1)[0] not in COMMAND_NAMES])

    def test_the_old_shell_value_is_read_as_the_new_one(self) -> None:
        record = bots.BotRecord.from_json({"bot_id": 1, "username": "x_bot", "name": "X", "owner_id": 9,
                                           "permissions": {"shell": "alfred", "web": "on"}})
        self.assertEqual(record.permissions, {"web": "on"})  # the old default is the new default
        self.assertEqual(record.permission("shell"), COMMAND)
        self.assertEqual(bots.clean_permission(self.sandbox.config, "shell", "alfred"), COMMAND)
        self.assertEqual(bots.clean_permission(self.sandbox.config, "shell", "full"), "full")
        with self.assertRaises(bots.BotError):
            bots.clean_permission(self.sandbox.config, "shell", "consigliere")

    def test_the_old_launcher_is_kept_but_never_created(self) -> None:
        bin_dir = self.sandbox.home / "bin"
        home.prepare(self.sandbox.config)
        self.assertFalse((bin_dir / "alfred").exists())  # a new home has only the new command
        new = (bin_dir / "consiglieri").read_text(encoding="utf-8")
        self.assertIn("-m consiglieri", new)
        self.assertIn("export CONSIGLIERI_HOME=", new)
        # A home from before the rename: its old launcher is rewritten as the same script.
        (bin_dir / "alfred").write_text("#!/bin/sh\nexport ALFRED_HOME=x\nexec python -m alfred \"$@\"\n")
        home.prepare(self.sandbox.config)
        self.assertEqual((bin_dir / "alfred").read_text(encoding="utf-8"), new)
        self.assertTrue(os.access(bin_dir / "alfred", os.X_OK))

    def test_new_bots_of_an_old_panel_get_the_new_name(self) -> None:
        self.assertEqual(bots.username_prefix("alfred_rnfl_bot"), "consiglieri_rnfl")
        self.assertEqual(bots.username_prefix("@Alfred_bot"), "consiglieri")
        self.assertEqual(bots.username_prefix("alfredo_bot"), "alfredo")  # someone's name, not the old one
        self.assertEqual(bots.suggest_username("Research", "alfred_rnfl_bot"), "consiglieri_rnfl_research_bot")

    def test_an_extension_shadows_neither_name(self) -> None:
        _, config = self.sandbox.add_bot()
        for name in COMMAND_NAMES:
            created = extensions.scaffold(config, "tool-%s" % name, "a tool")
            command = created.path / "bin" / name
            command.parent.mkdir(parents=True, exist_ok=True)
            command.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            command.chmod(0o755)
            errors = extensions.check(config, created.name, run_tests=False)["errors"]
            with self.subTest(name=name):
                self.assertIn("bin/%s would replace one of Consiglieri's own commands" % name, errors)


class LegacyServiceTests(unittest.TestCase):
    """The service installed before the rename: replaced by install, acted on until then. No real launchctl."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.config = self.sandbox.config
        self.calls: List[tuple] = []
        fake_home = self.sandbox.root / "user-home"
        for patcher in (mock.patch.object(service.Path, "home", return_value=fake_home),
                        mock.patch.object(service, "platform", return_value="launchd"),
                        mock.patch.object(service, "_launchctl", side_effect=self.launchctl),
                        mock.patch.object(service, "_systemctl", side_effect=AssertionError("systemctl"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agents = fake_home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)

    def launchctl(self, *args: str):
        self.calls.append(args)
        return mock.Mock(returncode=0, stdout="state = running\npid = 42\n")

    def test_the_label_acted_on(self) -> None:
        self.assertEqual(service.active_label(), "com.consiglieri.bot")
        (self.agents / "com.alfred.bot.plist").write_bytes(b"old")
        self.assertEqual(service.active_label(), "com.alfred.bot")
        service.restart()
        self.assertEqual(self.calls[-1][-1].rsplit("/", 1)[-1], "com.alfred.bot")
        self.assertIn("old name", service.status())
        (self.agents / "com.consiglieri.bot.plist").write_bytes(b"new")
        self.assertEqual(service.active_label(), "com.consiglieri.bot")
        self.assertNotIn("old name", service.status())

    def test_install_removes_the_old_service_first(self) -> None:
        (self.agents / "com.alfred.bot.plist").write_bytes(b"old")
        with mock.patch.object(service, "preflight", return_value=[]):
            path, notes = service.install(self.config)
        self.assertFalse((self.agents / "com.alfred.bot.plist").exists())
        self.assertEqual(path, self.agents / "com.consiglieri.bot.plist")
        targets = [call[1].rsplit("/", 1)[-1] for call in self.calls if call[0] in ("bootout", "bootstrap")]
        self.assertEqual(targets[0], "com.alfred.bot")  # stopped before the new one is loaded
        self.assertLess([c[0] for c in self.calls].index("bootout"), [c[0] for c in self.calls].index("bootstrap"))
        self.assertIn("old name", notes[0])
        plist = plistlib.loads(path.read_bytes())
        self.assertEqual(plist["Label"], "com.consiglieri.bot")
        self.assertEqual(plist["ProgramArguments"][-2:], ["consiglieri", "run"])
        self.assertIn("CONSIGLIERI_HOME", plist["EnvironmentVariables"])
        self.assertNotIn("ALFRED_HOME", plist["EnvironmentVariables"])

    def test_the_old_log_is_read_until_reinstalled(self) -> None:
        self.config.state_dir.mkdir(parents=True)
        (self.config.state_dir / "alfred.log").write_text("old line\n", encoding="utf-8")
        self.assertIn("old line", service.tail_log(self.config))
        service.log_path(self.config).write_text("new line\n", encoding="utf-8")
        self.assertIn("new line", service.tail_log(self.config))


class LegacyHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.user = self.sandbox.root / "user"
        self.user.mkdir()
        for patcher in (mock.patch.object(config_module, "DEFAULT_HOME", self.user / ".consiglieri"),
                        mock.patch.object(config_module, "LEGACY_HOME", self.user / ".alfred")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_home(self) -> None:
        self.assertEqual(Config.load({}).home, (self.user / ".consiglieri").resolve())
        (self.user / ".alfred").mkdir()
        loaded = Config.load({})
        self.assertEqual(loaded.home, (self.user / ".alfred").resolve())  # an old install keeps working
        self.assertEqual(loaded.bots_home, (self.user / ".alfred-bots").resolve())
        (self.user / ".consiglieri").mkdir()
        self.assertEqual(Config.load({}).home, (self.user / ".consiglieri").resolve())

    def test_a_linked_home_is_resolved_like_a_set_one(self) -> None:
        (self.user / ".consiglieri").mkdir()
        (self.user / ".alfred").symlink_to(self.user / ".consiglieri")
        loaded = Config.load({"ALFRED_HOME": str(self.user / ".alfred")})
        self.assertEqual(loaded.home, (self.user / ".consiglieri").resolve())
        self.assertEqual(loaded.shared_dir, (self.user / ".consiglieri-shared").resolve())


@unittest.skipUnless(test_credentials.HAS_CLI, "keepassxc-cli is not installed")
class LegacyIdentityTests(test_credentials.Base):
    """A bot's KeePassXC identity made before the rename keeps its name, so its grants still hold."""

    def test_an_old_identity_keeps_working(self) -> None:
        store = keepass.KeePass(self.config)
        store.setup()
        store.add("tum", urls=["moodle.tum.de"], username="u", password="old-secret")
        bot_a, bot_b = test_credentials.BOT_A, test_credentials.BOT_B
        old = {"id": "alfred-%d" % bot_a, "key": "a2V5"}
        (store.dir / "identities.json").write_text(json.dumps({str(bot_a): old}), encoding="utf-8")
        self.assertEqual(keepass.bot_of("alfred-%d" % bot_a), bot_a)
        self.assertTrue(store.grant("tum", bot_a))
        self.assertTrue(store.grant("tum", bot_b))
        names = set(store._identities_in(store.export(store.path("tum"))))
        self.assertEqual(names, {"alfred-%d" % bot_a, "consiglieri-%d" % bot_b})
        self.assertEqual(store.get("tum").bots, sorted([bot_a, bot_b]))
        self.assertIn("old-secret", store.secrets_for(bot_a))
        self.assertTrue(store.revoke("tum", bot_a))
        self.assertEqual(store.get("tum").bots, [bot_b])
        store.grant("tum", bot_a)
        self.assertEqual(store.forget_bot(bot_a), ["tum"])
        self.assertEqual(store.get("tum").bots, [bot_b])


def matches(rule: str, command: str) -> bool:
    """Whether a Claude Code Bash rule matches a command: `Bash(<pattern>)`, `*` for anything."""
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    pattern = "^" + ".*".join(re.escape(part) for part in rule[5:-1].split("*")) + "$"
    return re.match(pattern, command) is not None


if __name__ == "__main__":
    unittest.main()
