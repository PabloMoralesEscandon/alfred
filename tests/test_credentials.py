"""Credentials in KeePassXC, filled into each bot's own browser (docs/CREDENTIALS.md).

The store tests use the real keepassxc-cli when it is installed (they write throwaway databases in a temporary home)
and are skipped otherwise. Nothing here starts KeePassXC's app or a browser."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from alfred import bot_admin, bot_browser, bots, browser_cli, creds_cli, home, keepass, menus, presets
from alfred.bot_browser import BotBrowser, BrowserError
from alfred.bots import BotRecord, BotRegistry
from alfred.cli import CommandError, main
from alfred.keepass import CredentialError, KeePass
from fakes import Sandbox

HAS_CLI = bool(keepass._find([str(keepass.MAC_APP / "keepassxc-cli"), "keepassxc-cli"]))
BOT_A, BOT_B = 7000000001, 7000000002
SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238's SHA1 test key, "12345678901234567890"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.environ["ALFRED_BROWSER"] = "bot"
        home.prepare(self.sandbox.config)
        self.registry = BotRegistry(self.sandbox.config)
        for bot_id, username in ((BOT_A, "bot_a"), (BOT_B, "bot_b")):
            self.registry.save(BotRecord(bot_id=bot_id, username=username, name=username, owner_id=99, chat_ids=[99],
                                         permissions={"browser": "on", "creds": "on"}))
        env = dict(os.environ)
        for name in ("ALFRED_BOT_ID", "ALFRED_BOT_KEY"):
            env.pop(name, None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def config(self):
        return self.sandbox.config

    def as_bot(self, bot_id: int):
        return mock.patch.dict(os.environ, {"ALFRED_BOT_ID": str(bot_id),
                                            "ALFRED_BOT_KEY": bots.bot_key(self.config, bot_id, create=True)})


class TotpAndNames(unittest.TestCase):
    def test_rfc_6238_vectors(self) -> None:
        uri = keepass.check_totp(SEED)
        self.assertEqual(keepass.totp_code(uri, 59), "287082")
        self.assertEqual(keepass.totp_code(uri, 1111111109), "081804")
        eight = "otpauth://totp/x?secret=%s&digits=8" % SEED
        self.assertEqual(keepass.totp_code(eight, 59), "94287082")
        self.assertEqual(keepass.totp_code(eight, 20000000000), "65353130")

    def test_bad_totp(self) -> None:
        for bad in ("", "otpauth://hotp/x?secret=%s" % SEED, "otpauth://totp/x?secret=%s&digits=5" % SEED,
                    "otpauth://totp/x?secret=1&"):
            with self.assertRaises(CredentialError, msg=bad):
                keepass.check_totp(bad)

    def test_names_and_urls(self) -> None:
        self.assertEqual(keepass.check_name("Tum"), "tum")
        for bad in ("", "1x", "a b", "a/b", "hub", "x" * 41):
            with self.assertRaises(CredentialError, msg=bad):
                keepass.check_name(bad)
        self.assertEqual(keepass.clean_url("moodle.tum.de"), "https://moodle.tum.de")
        self.assertEqual(keepass.clean_url("http://localhost:8767"), "http://localhost:8767")
        for bad in ("http://example.com", "ftp://example.com", "javascript:alert(1)", ""):
            with self.assertRaises(CredentialError, msg=bad):
                keepass.clean_url(bad)

    def test_identity_names(self) -> None:
        self.assertEqual(keepass.identity_name(42), "alfred-42")
        self.assertEqual(keepass.bot_of("alfred-42"), 42)
        self.assertEqual(keepass.bot_of("chrome"), 0)
        self.assertEqual(keepass.bot_of("alfred-x"), 0)


@unittest.skipUnless(HAS_CLI, "keepassxc-cli is not installed")
class StoreTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.store = KeePass(self.config)
        self.store.setup()

    def add(self, name: str = "tum", **extra: Any):
        values = dict(urls=["moodle.tum.de", "https://login.tum.de"], username="ge12abc", password='p&ss<w"rd',
                      totp=SEED)
        values.update(extra)
        return self.store.add(name, **values)

    def test_setup_is_private_and_idempotent(self) -> None:
        self.assertEqual(self.store.setup(), [])
        self.assertEqual(stat.S_IMODE(self.store.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.key_file.stat().st_mode), 0o400)
        self.assertIn("SearchInAllDatabases=true", self.store.settings_file.read_text())
        self.assertIn("AllowGetDatabaseEntriesRequest=false", self.store.settings_file.read_text())
        self.assertIn("LockDatabaseIdle=false", self.store.settings_file.read_text())

    def test_add_describes_without_secrets(self) -> None:
        credential = self.add()
        self.assertEqual(credential.describe(), {"name": "tum", "username": "ge12abc", "totp": True, "bots": [],
                                                 "urls": ["https://moodle.tum.de", "https://login.tum.de"]})
        self.assertNotIn("rd", json.dumps(credential.describe()).replace("username", ""))
        self.assertEqual(stat.S_IMODE(self.store.path("tum").stat().st_mode), 0o600)
        with self.assertRaises(CredentialError):
            self.add()  # exists
        with self.assertRaises(CredentialError):
            self.store.add("other", urls=[], username="u", password="p")
        with self.assertRaises(CredentialError):
            self.store.add("other", urls=["x.org"], username="u", password="")
        # what keepassxc-cli sees: the right password, the extra URL and the TOTP
        cli = self.store.cli()
        shown = self.store._cli("show", *self.store._open_args(), "-s", "--all", str(self.store.path("tum")), "tum")
        self.assertIn('Password: p&ss<w"rd', shown)
        self.assertIn("KP2A_URL_1: https://login.tum.de", shown)
        self.assertTrue(cli)

    def test_no_plain_xml_left_behind(self) -> None:
        self.add()
        self.store.grant("tum", BOT_A)
        leftovers = [path.name for path in self.store.dir.iterdir() if path.suffix not in (".kdbx", ".key", ".ini", ".json")]
        self.assertEqual(leftovers, [])

    def test_grant_revoke_and_scope(self) -> None:
        self.add()
        self.add("mail", urls=["mail.example.org"], username="me", password="mail-secret", totp="")
        self.assertTrue(self.store.grant("tum", BOT_A))
        self.assertFalse(self.store.grant("tum", BOT_A))  # already
        self.assertEqual(self.store.get("tum").bots, [BOT_A])
        self.assertEqual([c.name for c in self.store.granted_to(BOT_A)], ["tum"])
        self.assertEqual(self.store.granted_to(BOT_B), [])
        hub = self.store._identities_in(self.store.export(self.store.hub))
        self.assertEqual(list(hub), ["alfred-%d" % BOT_A])
        self.assertEqual(hub["alfred-%d" % BOT_A], self.store.identity(BOT_A)["key"])
        mail = self.store._identities_in(self.store.export(self.store.path("mail")))
        self.assertEqual(mail, {})  # the identity is only where it was granted
        secrets = self.store.secrets_for(BOT_A)
        self.assertIn('p&ss<w"rd', secrets)
        self.assertIn(keepass.totp_code(keepass.check_totp(SEED)), secrets)
        self.assertNotIn("mail-secret", secrets)
        self.assertEqual(self.store.secrets_for(BOT_B), [])
        self.assertTrue(self.store.revoke("tum", BOT_A))
        self.assertFalse(self.store.revoke("tum", BOT_A))
        self.assertEqual(self.store.get("tum").bots, [])
        with self.assertRaises(CredentialError):
            self.store.grant("nope", BOT_A)

    def test_replace_keeps_grants(self) -> None:
        self.add()
        self.store.grant("tum", BOT_A)
        credential = self.add(password="new-one", totp="", replace=True)
        self.assertEqual(credential.bots, [BOT_A])
        self.assertFalse(credential.totp)
        self.assertIn("new-one", self.store.secrets_for(BOT_A))

    def test_forget_bot(self) -> None:
        self.add()
        self.store.grant("tum", BOT_A)
        self.store.grant("tum", BOT_B)
        self.assertEqual(self.store.forget_bot(BOT_A), ["tum"])
        self.assertEqual(self.store.get("tum").bots, [BOT_B])
        self.assertEqual(list(self.store._identities_in(self.store.export(self.store.hub))), ["alfred-%d" % BOT_B])
        self.assertIsNone(self.store.identity(BOT_A, create=False))

    def test_destroying_a_bot_takes_it_out(self) -> None:
        self.add()
        self.store.grant("tum", BOT_A)
        self.registry.remove(BOT_A)
        self.assertEqual(self.store.get("tum").bots, [])

    def test_remove(self) -> None:
        self.add()
        self.assertEqual(self.store.remove("tum").name, "tum")
        self.assertEqual(self.store.names(), [])
        with self.assertRaises(CredentialError):
            self.store.get("tum")

    def test_databases_open_with_the_hub_last(self) -> None:
        self.add("b")
        self.add("a")
        self.assertEqual([path.stem for path in self.store.databases()], ["a", "b", "hub"])


class AppTests(Base):
    def test_other_instances_and_running(self) -> None:
        store = KeePass(self.config)
        store.dir.mkdir(parents=True)
        store.pid_file.write_text("500\n")
        listing = ("  500 /Applications/KeePassXC.app/Contents/MacOS/KeePassXC --config %s --keyfile k\n"
                   "  600 /Applications/KeePassXC.app/Contents/MacOS/KeePassXC /Users/me/mine.kdbx\n"
                   "  700 /Applications/KeePassXC.app/Contents/MacOS/keepassxc-proxy chrome-extension://x/\n"
                   "  800 /usr/bin/vim keepassxc.ini\n" % store.settings_file)

        def run(argv, **kwargs):
            if argv[:2] == ["ps", "-p"]:
                return mock.Mock(stdout=listing.splitlines()[0].split(" ", 3)[-1], returncode=0)
            return mock.Mock(stdout=listing, returncode=0)

        store.run = run
        self.assertTrue(store.running())
        self.assertEqual([line.split(" ", 1)[0] for line in store.other_instances()], ["600"])

    def test_start_refuses_with_another_keepassxc_and_never_without_setup(self) -> None:
        store = KeePass(self.config)
        with self.assertRaises(CredentialError):
            store.start()


class BrowserTests(Base):
    def test_extension_id_is_chromiums(self) -> None:
        path = Path(self.sandbox.root)
        digest = hashlib.sha256(os.path.realpath(str(path)).encode()).hexdigest()[:32]
        expected = "".join("abcdefghijklmnop"[int(c, 16)] for c in digest)
        self.assertEqual(bot_browser.extension_id(path), expected)
        self.assertRegex(expected, r"^[a-p]{32}$")

    def test_scrub(self) -> None:
        text = "user ge12abc pass hunter22 code 123456 and hunter2"
        self.assertEqual(bot_browser.scrub(text, ["hunter2", "hunter22", "123456", "", "ab"]),
                         "user ge12abc pass [hidden] code [hidden] and [hidden]")

    def zipped(self, files: Dict[str, str]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, text in files.items():
                archive.writestr(name, text)
        return buffer.getvalue()

    def test_extension_install_checks_the_hash_and_paths(self) -> None:
        good = self.zipped({"manifest.json": "{}"})
        with mock.patch.object(bot_browser, "EXTENSION_SHA256", hashlib.sha256(good).hexdigest()):
            target = bot_browser.install_extension(self.config, fetch=lambda url: good)
            self.assertTrue((target / "manifest.json").is_file())
            self.assertEqual(bot_browser.install_extension(self.config, fetch=lambda url: b"never fetched"), target)
        __import__("shutil").rmtree(str(target))
        with self.assertRaises(BrowserError):
            bot_browser.install_extension(self.config, fetch=lambda url: good)  # the real pinned hash differs
        evil = self.zipped({"../../escape.txt": "x"})
        with mock.patch.object(bot_browser, "EXTENSION_SHA256", hashlib.sha256(evil).hexdigest()):
            with self.assertRaises(BrowserError):
                bot_browser.install_extension(self.config, fetch=lambda url: evil)
        self.assertFalse((self.config.state_dir / "escape.txt").exists())

    def test_arguments(self) -> None:
        browser = BotBrowser(self.config, BOT_A)
        args = browser.arguments(9000, True, None)
        self.assertIn("--headless=new", args)
        self.assertIn("--disable-extensions", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--user-data-dir=%s" % browser.profile, args)
        extension = Path("/x/ext")
        args = browser.arguments(9000, False, extension)
        self.assertNotIn("--headless=new", args)
        self.assertIn("--load-extension=/x/ext", args)
        self.assertIn("--disable-extensions-except=/x/ext", args)

    def test_profile_manifest_only_with_creds(self) -> None:
        proxy = self.sandbox.root / "keepassxc-proxy"
        proxy.write_text("#!/bin/sh\n")
        proxy.chmod(0o755)
        self.sandbox.environ["ALFRED_KEEPASSXC_PROXY"] = str(proxy)
        browser = BotBrowser(self.config, BOT_A)
        extension = bot_browser.extension_dir(self.config)
        extension.mkdir(parents=True)
        (extension / "manifest.json").write_text("{}")
        self.assertEqual(browser._prepare_profile(True), extension)
        manifest = json.loads((browser.profile / "NativeMessagingHosts" / ("%s.json" % bot_browser.NATIVE_HOST)).read_text())
        self.assertEqual(manifest["path"], str(proxy))
        self.assertEqual(manifest["allowed_origins"], ["chrome-extension://%s/" % bot_browser.extension_id(extension)])
        self.assertEqual(stat.S_IMODE(browser.dir.stat().st_mode), 0o700)
        preferences = json.loads((browser.profile / "Default" / "Preferences").read_text())
        self.assertFalse(preferences["credentials_enable_service"])
        self.assertIsNone(browser._prepare_profile(False))
        self.assertFalse((browser.profile / "NativeMessagingHosts" / ("%s.json" % bot_browser.NATIVE_HOST)).exists())

    def test_refs(self) -> None:
        for good in ("e3", "e12", "f1e3"):
            self.assertEqual(bot_browser.check_ref(good), good)
        for bad in ("", "3", "e", "e3; rm", "body", "aria-ref=e3"):
            with self.assertRaises(BrowserError, msg=bad):
                bot_browser.check_ref(bad)

    def test_state_without_a_browser(self) -> None:
        browser = BotBrowser(self.config, BOT_A)
        self.assertEqual(browser.status(), {"running": False, "headless": None, "credentials": None})
        self.assertFalse(browser.stop())


class CommandTests(Base):
    def run_json(self, *argv: str) -> Dict[str, Any]:
        with mock.patch.dict(os.environ, self.sandbox.environ):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                main(list(argv))
        return json.loads(out.getvalue())

    def test_owner_commands_refused_in_a_bot_turn(self) -> None:
        with self.as_bot(BOT_A):
            for argv in (["setup"], ["add", "x", "--url", "a.org"], ["remove", "x"], ["grant", "x", "@bot_a"],
                         ["revoke", "x", "@bot_a"], ["start"], ["stop"]):
                result = self.run_json("creds", *argv)
                self.assertFalse(result["ok"], argv)
                self.assertIn("for the user", result["error"])
            result = self.run_json("browser", "install")
            self.assertFalse(result["ok"])

    def test_add_needs_a_terminal(self) -> None:
        KeePass(self.config).dir.mkdir(parents=True)
        with mock.patch.object(KeePass, "require_set_up"):
            with mock.patch("sys.stdin", io.StringIO("secret\n")):
                result = self.run_json("creds", "add", "x", "--url", "a.org")
        self.assertFalse(result["ok"])
        self.assertIn("terminal", result["error"])

    def test_list_before_setup(self) -> None:
        self.assertEqual(self.run_json("creds", "list")["credentials"], [])

    @unittest.skipUnless(HAS_CLI, "keepassxc-cli is not installed")
    def test_list_in_a_bot_turn_shows_only_its_own(self) -> None:
        store = KeePass(self.config)
        store.setup()
        store.add("tum", ["tum.de"], "ge12abc", "secret-pass")
        store.add("mail", ["mail.org"], "me", "mail-pass")
        result = self.run_json("creds", "grant", "tum", "@bot_a")
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        with self.as_bot(BOT_A):
            mine = self.run_json("creds", "list")
        self.assertEqual([c["name"] for c in mine["credentials"]], ["tum"])
        self.assertNotIn("bots", mine["credentials"][0])
        self.assertNotIn("secret-pass", json.dumps(mine))
        with self.as_bot(BOT_B):
            self.assertEqual(self.run_json("creds", "list")["credentials"], [])
        with mock.patch.dict(os.environ, {"ALFRED_BOT_ID": str(BOT_B), "ALFRED_BOT_KEY": "wrong"}):
            self.assertFalse(self.run_json("creds", "list")["ok"])
        owner = self.run_json("creds", "list")
        self.assertEqual({c["name"]: c["bots"] for c in owner["credentials"]}, {"mail": [], "tum": ["@bot_a"]})
        audit = bots.read_audit(self.config)
        self.assertEqual([(e["event"], e["credential"]) for e in audit], [("credential_granted", "tum")])
        self.assertIn("credential tum", bots.audit_line(audit[0]))

    def test_grant_notes_what_is_missing(self) -> None:
        record = self.registry.get(BOT_B)
        record.permissions = {}
        self.registry.save(record)
        notes = creds_cli._bot_warnings(self.config, self.registry.get(BOT_B))
        self.assertEqual(len(notes), 2)  # creds and browser
        record.permissions = {"browser": "on", "creds": "on"}
        self.registry.save(record)
        self.sandbox.environ["ALFRED_BROWSER"] = "orca"
        notes = creds_cli._bot_warnings(self.config, self.registry.get(BOT_B))
        self.assertEqual(len(notes), 1)
        self.assertIn("browser bot", notes[0])

    def test_browser_is_each_bots_own(self) -> None:
        with self.as_bot(BOT_A):
            result = self.run_json("browser", "tabs", "--bot", "@bot_b")
            self.assertFalse(result["ok"])
            self.assertIn("its own browser", result["error"])
        with mock.patch.dict(os.environ, {"ALFRED_BOT_ID": str(BOT_A), "ALFRED_BOT_KEY": "wrong"}):
            self.assertIn("ALFRED_BOT_KEY", self.run_json("browser", "tabs")["error"])
        self.assertIn("--bot", self.run_json("browser", "tabs")["error"])

    def test_browser_needs_the_permission_and_the_mode(self) -> None:
        record = self.registry.get(BOT_A)
        record.permissions = {"creds": "on"}
        self.registry.save(record)
        with self.as_bot(BOT_A):
            self.assertIn("browser permission", self.run_json("browser", "tabs")["error"])
        record.permissions = {"browser": "on"}
        self.registry.save(record)
        self.sandbox.environ["ALFRED_BROWSER"] = "orca"
        with self.as_bot(BOT_A):
            self.assertIn("Orca's browser", self.run_json("browser", "tabs")["error"])
        self.assertIn("Orca's browser", self.run_json("browser", "tabs", "--bot", "@bot_a")["error"])
        record.settings = {"browser": "bot"}  # its own setting wins over .env's, from a terminal too
        self.registry.save(record)
        result = self.run_json("browser", "status", "--bot", "@bot_a")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["running"])

    def test_creds_list_needs_the_permission(self) -> None:
        record = self.registry.get(BOT_A)
        record.permissions = {"browser": "on"}
        self.registry.save(record)
        KeePass(self.config).dir.mkdir(parents=True)
        with mock.patch.object(KeePass, "is_set_up", return_value=True):
            with self.as_bot(BOT_A):
                result = self.run_json("creds", "list")
        self.assertFalse(result["ok"])
        self.assertIn("creds permission", result["error"])


class BrowserSettingTests(Base):
    def test_each_bot_may_have_its_own(self) -> None:
        self.sandbox.environ["ALFRED_BROWSER"] = "chrome"
        self.assertEqual(bots.clean_setting(self.config, "browser", "Bot"), "bot")
        self.assertEqual(bots.clean_setting(self.config, "browser", "default"), "")
        with self.assertRaises(bots.BotError):
            bots.clean_setting(self.config, "browser", "firefox")
        record = self.registry.get(BOT_A)
        record.settings = {"browser": "bot"}
        self.registry.save(record)
        self.assertEqual(self.registry.config_for(BOT_A).browser, "bot")
        self.assertEqual(self.registry.config_for(BOT_B).browser, "chrome")  # the others keep .env's
        record.permissions = {}
        self.registry.save(record)
        self.assertEqual(self.registry.config_for(BOT_A).browser, "none")  # no permission, no browser


class PermissionTests(Base):
    def rules(self, **permissions: str) -> Dict[str, Any]:
        record = self.registry.get(BOT_A)
        record.permissions = dict(permissions)
        self.registry.save(record)
        return bots.claude_settings(self.registry.config_for(BOT_A), self.registry.get(BOT_A))

    def test_off_by_default_and_in_every_preset_and_template(self) -> None:
        self.assertEqual(bots.DEFAULT_PERMISSIONS["creds"], "off")
        self.assertIn("creds", bots.SWITCHES)
        for key in list(presets.keys()) + list(presets.templates()):
            self.assertNotEqual(presets.resolve(key).get("creds", "off"), "on", key)

    def test_rules(self) -> None:
        off = self.rules(browser="on")["permissions"]
        self.assertIn("Bash(alfred creds *)", off["deny"])
        on = self.rules(browser="on", creds="on")["permissions"]
        self.assertNotIn("Bash(alfred creds *)", on["deny"])
        self.assertIn("Bash(alfred browser *)", on["allow"])
        for settings in (off, on, self.rules(shell="full", mode="bypassPermissions", creds="on")["permissions"]):
            for rule in bots.CREDS_OWNER_RULES + bots.KEEPASS_RULES + ("Bash(*alfred browser install*)",):
                self.assertIn(rule, settings["deny"])
        no_browser = self.rules()["permissions"]
        self.assertIn("Bash(alfred browser *)", no_browser["deny"])

    def test_sandbox_lets_the_helpers_out(self) -> None:
        sandbox = self.rules(shell="full", browser="on", creds="on")["sandbox"]
        self.assertIn("alfred browser *", sandbox["excludedCommands"])
        self.assertIn("alfred creds list", sandbox["excludedCommands"])
        self.assertNotIn("alfred creds *", sandbox["excludedCommands"])
        sandbox = self.rules(shell="full")["sandbox"]
        self.assertNotIn("alfred browser *", sandbox["excludedCommands"])
        self.assertNotIn("alfred creds list", sandbox["excludedCommands"])

    def test_bot_admin_never_gives_it(self) -> None:
        admin = BotRecord(bot_id=1, username="admin_bot", name="Admin", owner_id=99,
                          permissions={"admin": "on", "creds": "on", "browser": "on"})
        self.registry.save(admin)
        with self.assertRaises(bot_admin.SpecError) as raised:
            bot_admin.parse_spec(self.config, admin, {"name": "X", "username": "x_new_bot",
                                                      "permissions": {"creds": "on"}})
        self.assertTrue(any("creds" in problem for problem in raised.exception.problems))

    def test_panel_and_manual(self) -> None:
        self.assertIn("creds", dict(menus.SWITCH_LABELS))
        record = self.registry.get(BOT_A)
        self.assertIn(menus.CREDS_WARNING, menus.permission_warnings(record))
        self.assertIn("credentials the user granted you", bots.permission_summary(record))
        record.permissions = {}
        self.assertNotIn(menus.CREDS_WARNING, menus.permission_warnings(record))
        self.assertEqual(presets.WORDS["creds"], "credentials")


if __name__ == "__main__":
    unittest.main()
