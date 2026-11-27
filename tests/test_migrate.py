"""`consiglieri migrate`, on a fake user folder: never the real home, service or Claude Code folder.

It names the old paths and settings on purpose: it is listed in `.rename-allow`.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Dict, List
from unittest import mock

from consiglieri import cli
from consiglieri.bots import BotRecord, claude_project_dir
from consiglieri.config import Config
from consiglieri.migrate import Migration, MigrationError, project_key, rename_settings, replace_paths

BOT = 5000000001


class MigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.user = Path(self._tempdir.name).resolve() / "user"
        self.old = self.user / ".alfred"
        self.new = self.user / ".consiglieri"
        self.claude = self.user / ".claude"
        self.repo_env = self.user / "repo.env"
        self.processes: List[str] = ["/usr/bin/login", "python -m http.server"]
        self.build()

    def write(self, path: Path, text: str, mode: int = 0o600) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def build(self) -> None:
        """An install as it is before the rename, with a test instance next to it that must stay as it is."""
        old, bots, shared = self.old, self.user / ".alfred-bots", self.user / ".alfred-shared"
        self.write(old / ".env", "TELEGRAM_BOT_TOKEN=123:secret\nALFRED_ALLOWED_USERS=99\n"
                                 "export ALFRED_TIMEOUT=30 # seconds\nCONSIGLIERI_BACKEND=claude\nALFRED_BACKEND=codex\n")
        record = BotRecord(bot_id=BOT, username="helper_bot", name="Helper", owner_id=99,
                           settings={"workdir": str(bots / str(BOT) / "work")},
                           permissions={"shell": "alfred", "folders": "%s,%s" % (shared / "memory", self.user / ".alfred-test")})
        data = dataclasses.asdict(record)
        self.write(old / "state" / "bots" / str(BOT) / "bot.json", json.dumps(data, indent=2))
        self.write(old / "state" / "bots" / str(BOT) / "token", "%d:token" % BOT)
        self.write(old / "state" / "credentials" / "keepassxc-local.ini",
                   "[General]\nLastDatabases=%s/state/credentials/hub.kdbx\n" % old)
        self.write(old / "state" / "browsers" / str(BOT) / "run.json", json.dumps({"pid": 999999999}))
        self.write(old / "state" / "browsers" / str(BOT) / "profile" / "Default" / "Preferences",
                   json.dumps({"path": str(old)}))
        self.write(old / "state" / "scheduler.sqlite3", "not text: %s" % old)
        self.write(old / "bin" / "alfred", "#!/bin/sh\nexport ALFRED_HOME=%s\nexec python -m alfred \"$@\"\n" % old, 0o755)
        self.write(bots / str(BOT) / "memory" / "MEMORY.md", "# Memory\n")
        self.write(shared / "memory" / "MEMORY.md", "# Shared\n")
        self.write(self.user / ".alfred-test" / ".env", "ALFRED_HOME=%s/.alfred-test\n" % self.user)
        for folder in (old, bots / str(BOT), self.user / ".alfred-test", self.user / ".alfred-test-bots" / "7"):
            self.write(self.claude / "projects" / project_key(folder) / "session.jsonl", '{"cwd": "%s"}\n' % folder)
        self.write(self.repo_env, "ALFRED_HOME=%s\nALFRED_ENV_FILE_UNUSED=1\n" % old)

    def config(self, environ: Dict[str, str] = None) -> Config:
        environ = dict(environ or {}, CLAUDE_CONFIG_DIR=str(self.claude), PATH=os.environ.get("PATH", ""))
        return Config(home=self.old, env_files=[self.old / ".env", self.repo_env], environ=environ)

    def migration(self, environ: Dict[str, str] = None, **extra) -> Migration:
        return Migration(self.config(environ), user_home=self.user, processes=lambda: list(self.processes), **extra)

    def snapshot(self) -> Dict[str, str]:
        return {str(path.relative_to(self.user)): (path.read_text(encoding="utf-8") if path.is_file() else "")
                for path in sorted(self.user.rglob("*"))}

    def run_clean(self, environ: Dict[str, str] = None, **extra) -> List[str]:
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("CONSIGLIERI_BOT_ID", "CONSIGLIERI_BOT_KEY", "ALFRED_BOT_ID", "ALFRED_BOT_KEY"):
                os.environ.pop(name, None)
            return self.migration(environ, **extra).run(dry_run=False)

    # -- helpers --

    def test_paths_are_whole_components(self) -> None:
        pairs = [(self.old, self.new), (self.user / ".alfred-bots", self.user / ".consiglieri-bots")]
        text = "%s %s/x %s-bots/5 %s-test %s.bak" % ((self.old,) * 5)
        self.assertEqual(replace_paths(text, pairs), "%s %s/x %s-bots/5 %s-test %s.bak" % (
            self.new, self.new, self.new, self.old, self.old))

    def test_rename_settings_keeps_values_and_new_names(self) -> None:
        text, names = rename_settings("ALFRED_A=1\nexport ALFRED_B='x y' # c\nCONSIGLIERI_C=new\nALFRED_C=old\n#ALFRED_D=1\n")
        self.assertEqual(text, "CONSIGLIERI_A=1\nexport CONSIGLIERI_B='x y' # c\nCONSIGLIERI_C=new\nALFRED_C=old\n#ALFRED_D=1\n")
        self.assertEqual(names, ["ALFRED_A", "ALFRED_B"])

    # -- dry run --

    def test_dry_run_changes_nothing_and_shows_no_secret(self) -> None:
        before = self.snapshot()
        with mock.patch.dict(os.environ, {"CONSIGLIERI_HOME": str(self.old)}):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("consiglieri.migrate.Path.home", return_value=self.user), \
                    mock.patch("consiglieri.migrate._processes", return_value=[]), \
                    mock.patch.object(Config, "load", return_value=self.config()), \
                    redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["migrate", "--dry-run"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.snapshot(), before)
        text = out.getvalue()
        self.assertIn("nothing has changed", text)
        self.assertIn("move %s -> %s" % (self.old, self.new), text)
        self.assertIn("move %s -> %s" % (self.user / ".alfred-bots", self.user / ".consiglieri-bots"), text)
        self.assertIn(project_key(self.user / ".consiglieri-bots" / str(BOT)), text)
        self.assertIn("ALFRED_ALLOWED_USERS, ALFRED_TIMEOUT", text)
        self.assertNotIn("secret", text)
        self.assertNotIn(".alfred-test", text)

    # -- the real thing --

    def test_migrates_everything(self) -> None:
        test_instance = {key: value for key, value in self.snapshot().items() if ".alfred-test" in key}
        env_mode = stat.S_IMODE((self.old / ".env").stat().st_mode)
        done = self.run_clean()
        self.assertTrue(done)
        # The three folders, and nothing of the test instance.
        for name in (".alfred", ".alfred-bots", ".alfred-shared"):
            self.assertFalse((self.user / name).exists(), name)
            self.assertTrue((self.user / name.replace("alfred", "consiglieri")).is_dir(), name)
        self.assertFalse((self.user / ".alfred").is_symlink())
        self.assertEqual({key: value for key, value in self.snapshot().items() if ".alfred-test" in key}, test_instance)
        # Claude Code's folders: where claude_project_dir looks for the moved home and bot, sessions inside.
        new_config = Config(home=self.new, env_files=[], environ={"CLAUDE_CONFIG_DIR": str(self.claude)})
        for folder in (self.new, self.user / ".consiglieri-bots" / str(BOT)):
            self.assertTrue((claude_project_dir(new_config, folder) / "session.jsonl").is_file(), folder)
        self.assertFalse((self.claude / "projects" / project_key(self.old)).exists())
        self.assertTrue((self.claude / "projects" / project_key(self.user / ".alfred-test")).exists())
        # The bot's settings: new paths, the test instance's left alone, the new shell value; it still loads.
        bot = json.loads((self.new / "state" / "bots" / str(BOT) / "bot.json").read_text(encoding="utf-8"))
        self.assertEqual(bot["settings"]["workdir"], str(self.user / ".consiglieri-bots" / str(BOT) / "work"))
        self.assertEqual(bot["permissions"]["folders"], "%s,%s" % (self.user / ".consiglieri-shared" / "memory",
                                                                   self.user / ".alfred-test"))
        self.assertEqual(bot["permissions"]["shell"], "consiglieri")
        self.assertEqual(BotRecord.from_json(bot).permission("shell"), "consiglieri")
        self.assertEqual((self.new / "state" / "bots" / str(BOT) / "token").read_text(), "%d:token" % BOT)
        # KeePassXC's settings point at the moved databases; the binary database and the profiles are untouched.
        self.assertIn(str(self.new / "state" / "credentials" / "hub.kdbx"),
                      (self.new / "state" / "credentials" / "keepassxc-local.ini").read_text())
        self.assertIn(str(self.old), (self.new / "state" / "scheduler.sqlite3").read_text())
        self.assertIn(str(self.old), (self.new / "state" / "browsers" / str(BOT) / "profile" / "Default" / "Preferences").read_text())
        # The .env files: names renamed, values kept, a name already set under the new one left, mode kept.
        env = (self.new / ".env").read_text(encoding="utf-8")
        self.assertEqual(env, "TELEGRAM_BOT_TOKEN=123:secret\nCONSIGLIERI_ALLOWED_USERS=99\n"
                              "export CONSIGLIERI_TIMEOUT=30 # seconds\nCONSIGLIERI_BACKEND=claude\nALFRED_BACKEND=codex\n")
        self.assertEqual(stat.S_IMODE((self.new / ".env").stat().st_mode), env_mode)
        self.assertEqual(self.repo_env.read_text(), "CONSIGLIERI_HOME=%s\nCONSIGLIERI_ENV_FILE_UNUSED=1\n" % self.new)
        self.assertEqual(Config.load({"CONSIGLIERI_ENV_FILE": str(self.repo_env)}).home, self.new)
        # The launchers, both names, for the new home.
        for name in ("consiglieri", "alfred"):
            launcher = (self.new / "bin" / name).read_text(encoding="utf-8")
            self.assertIn("export CONSIGLIERI_HOME=%s" % self.new, launcher)
            self.assertIn("-m consiglieri", launcher)
        self.assertFalse(list(self.user.rglob("*.migrating")))

    def test_another_home(self) -> None:
        source, target = self.user / ".alfred-test", self.user / ".consiglieri-test"
        self.run_clean(source=source, target=target)
        self.assertTrue((target / ".env").is_file())
        self.assertFalse((self.user / ".consiglieri-test-bots").exists())  # it had no bots' folder
        self.assertTrue((self.claude / "projects" / project_key(target)).is_dir())
        self.assertTrue(self.old.is_dir())  # the real one untouched
        self.assertEqual((target / ".env").read_text(), "CONSIGLIERI_HOME=%s\n" % target)

    # -- refusals: nothing changes --

    def assert_refused(self, expected: str, environ: Dict[str, str] = None, **extra) -> None:
        before = self.snapshot()
        with self.assertRaises(MigrationError) as caught:
            self.run_clean(environ, **extra)
        self.assertIn(expected, str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_refuses_while_a_service_is_installed(self) -> None:
        for path in (self.user / "Library" / "LaunchAgents" / "com.alfred.bot.plist",
                     self.user / "Library" / "LaunchAgents" / "com.consiglieri.bot.plist",
                     self.user / ".config" / "systemd" / "user" / "alfred.service"):
            self.write(path, "x")
            self.assert_refused("consiglieri service uninstall")
            path.unlink()

    def test_refuses_while_it_runs(self) -> None:
        for line in ("/Users/x/venv/bin/python3 -m alfred run", "python -m consiglieri run",
                     "/Users/x/.alfred/bin/alfred run"):
            self.processes = [line]
            self.assert_refused("still running")

    def test_refuses_while_a_browser_is_open(self) -> None:
        self.write(self.old / "state" / "browsers" / str(BOT) / "run.json", json.dumps({"pid": os.getpid()}))
        self.assert_refused("browser is open")

    def test_refuses_when_a_destination_exists(self) -> None:
        (self.user / ".consiglieri-shared").mkdir()
        self.assert_refused(".consiglieri-shared already exists")
        (self.user / ".consiglieri-shared").rmdir()
        (self.claude / "projects" / project_key(self.new)).mkdir(parents=True)
        self.assert_refused("Claude Code's folder")

    def test_refuses_in_a_bots_turn(self) -> None:
        self.assert_refused("a bot never runs it", {"ALFRED_BOT_ID": str(BOT)})
        self.assert_refused("a bot never runs it", {"CONSIGLIERI_BOT_KEY": "k"})

    def test_refuses_without_an_old_home(self) -> None:
        self.assert_refused("nothing to migrate", source=self.user / ".nothing")


if __name__ == "__main__":
    unittest.main()
