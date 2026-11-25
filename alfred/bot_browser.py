"""Each bot's own browser: a Chromium with its own profile, driven with `alfred browser` (docs/CREDENTIALS.md).

With ALFRED_BROWSER=bot, a bot with the browser permission gets a browser of its own instead of Orca's or the
shared Chrome profile. It is a plain Chromium (Playwright's build, which still loads unpacked extensions),
started by Consiglieri on demand, and driven over the DevTools protocol by `alfred browser ...`: commands any
agent can run from a shell, whatever its harness or model.

    <home>/state/browsers/<bot_id>/       0700, closed to every bot
        profile/                          its Chromium profile: its cookies and sessions, no one else's
            NativeMessagingHosts/         how its extension reaches KeePassXC
        run.json                          the running browser: pid, port, whether it is headless, with credentials
        chromium.log
    <home>/state/browser-extensions/keepassxc-browser-<version>/   the extension, unpacked, checked by its hash

With the `creds` permission too, the browser loads KeePassXC-Browser with the bot's identity already in it
(alfred/keepass.py): the extension fills in the credentials granted to that bot, on their sites only, by
itself. Without it, no extension is loaded at all.

What the bot reads of a page passes through `scrub`: the values of password fields, and every password and
current TOTP code of the credentials granted to it, are replaced by [hidden]. There is no command to run
JavaScript or to read a field's value.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .config import Config
from .keepass import CredentialError, KeePass

EXTENSION_NAME = "keepassxc-browser"
EXTENSION_VERSION = "1.10.4.1"
EXTENSION_URL = ("https://github.com/keepassxreboot/keepassxc-browser/releases/download/{v}/"
                 "keepassxc-browser_{v}_chromium.zip").format(v=EXTENSION_VERSION)
EXTENSION_SHA256 = "3ccda612a951dd4788b424cd41adced832922d4e482ea3ee03c6013390f45842"
NATIVE_HOST = "org.keepassxc.keepassxc_browser"
HIDDEN = "[hidden]"
REF_PATTERN = re.compile(r"^[a-z]*\d*e\d+$")  # Playwright's aria refs: e12, or f1e3 inside a frame
START_SECONDS = 20
# The extension's settings: fill a login form by itself when one credential matches, and the TOTP field the same
# way; never submit by itself (the bot decides), and no banners or guides. (autoReconnect is not here: the
# extension turns it off itself every time its worker starts, and reconnects on the next page anyway.)
EXTENSION_SETTINGS = {
    "autoFillSingleEntry": True, "autoFillSingleTotp": True, "autoSubmit": False, "autoFillAndSend": False,
    "autoRetrieveCredentials": True, "showLoginNotifications": False,
    "showGettingStartedGuideAlert": False, "showTroubleshootingGuideAlert": False, "checkUpdateKeePassXC": 0,
    "passkeys": False,
}


class BrowserError(Exception):
    """Something the bot or the user can act on: not installed, a bad ref, a page that is gone."""


def mode(config: Config) -> str:
    return config.browser


def extension_dir(config: Config) -> Path:
    return config.state_dir / "browser-extensions" / ("%s-%s" % (EXTENSION_NAME, EXTENSION_VERSION))


def extension_id(path: Path) -> str:
    """The id Chromium gives an unpacked extension: from the hash of its absolute path, in letters a to p."""
    digest = hashlib.sha256(os.path.realpath(str(path)).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def scrub(text: str, secrets: Sequence[str]) -> str:
    """Hide every secret in a text a bot is about to read. Longest first, so no part of one is left showing."""
    for secret in sorted({value for value in secrets if value and len(value) >= 3}, key=len, reverse=True):
        text = text.replace(secret, HIDDEN)
    return text


def install_extension(config: Config, fetch: Callable[[str], bytes] = None) -> Path:
    """Download KeePassXC-Browser for Chromium, check it against the hash pinned above, and unpack it."""
    target = extension_dir(config)
    if (target / "manifest.json").is_file():
        return target
    data = (fetch or _fetch)(EXTENSION_URL)
    found = hashlib.sha256(data).hexdigest()
    if found != EXTENSION_SHA256:
        raise BrowserError("the KeePassXC-Browser download does not match its pinned hash (%s); not installing it" % found)
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        import shutil

        shutil.rmtree(str(partial))
    partial.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.namelist():
            destination = (partial / member).resolve()
            if not str(destination).startswith(str(partial.resolve()) + os.sep) and destination != partial.resolve():
                raise BrowserError("the extension archive has a path outside its folder: %s" % member)
        archive.extractall(str(partial))
    os.replace(str(partial), str(target))
    return target


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - a fixed https URL
        return response.read()


def install(config: Config) -> List[str]:
    """Everything the bots' browsers need: Playwright's Chromium and the extension. Idempotent."""
    done = []
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise BrowserError("Playwright is not installed: venv/bin/pip install -e '.[browser]'") from None
    process = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, check=False)
    if process.returncode != 0:
        raise BrowserError("playwright install chromium failed: %s" % process.stdout.strip().splitlines()[-1:])
    done.append("Chromium: %s" % chromium_path(config))
    done.append("KeePassXC-Browser %s: %s" % (EXTENSION_VERSION, install_extension(config)))
    return done


def chromium_path(config: Config) -> str:
    configured = config.get("ALFRED_CHROMIUM")
    if configured:
        return configured
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserError("Playwright is not installed: venv/bin/pip install -e '.[browser]', then "
                           "`alfred browser install`") from None
    with sync_playwright() as playwright:
        path = playwright.chromium.executable_path
    if not os.path.exists(path):
        raise BrowserError("Chromium is not installed; run `alfred browser install` on the machine")
    return path


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class BotBrowser:
    """One bot's browser. `popen` stands in for subprocess.Popen in tests."""

    def __init__(self, config: Config, bot_id: int, keepass: Optional[KeePass] = None,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.config = config
        self.bot_id = int(bot_id)
        self.dir = config.state_dir / "browsers" / str(self.bot_id)
        self.profile = self.dir / "profile"
        self.keepass = keepass or KeePass(config)
        self.popen = popen

    # -- its state --

    def _run_file(self) -> Path:
        return self.dir / "run.json"

    def state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._run_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def running(self) -> bool:
        state = self.state()
        if not _alive(int(state.get("pid") or 0)):
            return False
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % int(state["port"]), timeout=2):
                return True
        except (OSError, ValueError, KeyError):
            return False

    def status(self) -> Dict[str, Any]:
        state = self.state()
        running = self.running()
        return {"running": running, "headless": bool(state.get("headless", True)) if running else None,
                "credentials": bool(state.get("creds")) if running else None}

    # -- starting and stopping --

    def _private(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(str(path), 0o700)

    def _prepare_profile(self, creds: bool) -> Optional[Path]:
        """The profile folder, Chromium's own password manager off (KeePassXC fills, nothing is saved in the
        profile), and the native messaging manifest that lets the extension reach KeePassXC, only with creds."""
        self._private(self.dir)
        self._private(self.profile)
        preferences = self.profile / "Default" / "Preferences"
        if not preferences.exists():
            preferences.parent.mkdir(parents=True, exist_ok=True)
            preferences.write_text(json.dumps({"credentials_enable_service": False,
                                               "profile": {"password_manager_enabled": False}}), encoding="utf-8")
        manifest = self.profile / "NativeMessagingHosts" / ("%s.json" % NATIVE_HOST)
        if not creds:
            if manifest.exists():
                manifest.unlink()
            return None
        extension = install_extension(self.config)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "name": NATIVE_HOST, "description": "KeePassXC for Consiglieri's bot %d" % self.bot_id,
            "path": self.keepass.proxy(), "type": "stdio",
            "allowed_origins": ["chrome-extension://%s/" % extension_id(extension)],
        }, indent=2), encoding="utf-8")
        return extension

    def arguments(self, port: int, headless: bool, extension: Optional[Path]) -> List[str]:
        args = ["--user-data-dir=%s" % self.profile, "--remote-debugging-port=%d" % port,
                "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                "--disable-default-apps", "--disable-features=Translate,AutofillServerCommunication"]
        if sys.platform == "darwin":
            args.append("--use-mock-keychain")  # nothing of its profile goes to the user's Keychain, and no prompts
        else:
            args.append("--password-store=basic")
        if headless:
            args.append("--headless=new")
        if extension is not None:
            args += ["--load-extension=%s" % extension, "--disable-extensions-except=%s" % extension]
        else:
            args.append("--disable-extensions")
        return args + ["about:blank"]

    def ensure(self, creds: bool, headless: Optional[bool] = None) -> Dict[str, Any]:
        """Running, with or without credentials as the bot's permission says now. Starts or restarts it as needed.
        `headless` None keeps how it is (headless when it starts)."""
        state = self.state()
        running = self.running()
        if headless is None:
            headless = bool(state.get("headless", True)) if running else True
        if creds:
            self.keepass.start()  # no-op when it runs; the extension can only fill with the app open
        if running and bool(state.get("creds")) == creds and bool(state.get("headless", True)) == headless:
            return state
        urls = self.tab_urls() if running else []
        if running:
            self.stop()
        return self.start(creds, headless, urls)

    def start(self, creds: bool, headless: bool = True, urls: Sequence[str] = ()) -> Dict[str, Any]:
        extension = self._prepare_profile(creds)
        identity = self.keepass.connect(self.bot_id) if creds else None
        port = _free_port()
        log = open(str(self.dir / "chromium.log"), "ab")
        try:
            process = self.popen([chromium_path(self.config)] + self.arguments(port, headless, extension),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        finally:
            log.close()
        state = {"pid": process.pid, "port": port, "headless": headless, "creds": creds,
                 "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._run_file().write_text(json.dumps(state) + "\n", encoding="utf-8")
        os.chmod(str(self._run_file()), 0o600)
        deadline = time.time() + START_SECONDS
        while not self.running():
            if time.time() > deadline or not _alive(process.pid):
                raise BrowserError("the browser did not start; see %s" % (self.dir / "chromium.log"))
            time.sleep(0.2)
        if extension is not None and identity is not None:
            self._seed(extension_id(extension), identity)
        if urls:
            with self.session() as (_, context):
                for url in urls:
                    if url.startswith(("http://", "https://")):
                        context.new_page().goto(url, wait_until="domcontentloaded")
        return state

    def stop(self) -> bool:
        state = self.state()
        pid = int(state.get("pid") or 0)
        if not _alive(pid):
            if self._run_file().exists():
                self._run_file().unlink()
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.1)
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if self._run_file().exists():
            self._run_file().unlink()
        return True

    # -- the extension --

    def _seed(self, ext_id: str, identity: Dict[str, str]) -> None:
        """Give the extension the bot's identity and its settings, from inside it: what pressing Connect in its popup
        and saving its options would do, with no dialog for anyone to answer."""
        # The extension sets itself up when its worker starts, and that writes its settings back with the defaults:
        # wait until it has (page.settings is filled in), then write ours, and check they stayed.
        script = """async ([id, key, settings]) => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const applied = s => s && Object.entries(settings).every(([k, v]) => s[k] === v);
            for (let i = 0; i < 50 && !(page.settings && Object.keys(page.settings).length > 10); i++) await sleep(100);
            for (let i = 0; i < 10; i++) {
                const stored = (await browser.storage.local.get('settings')).settings || {};
                if (applied(stored) && applied(page.settings)) break;
                const merged = Object.assign({}, stored, settings);
                page.settings = merged;
                await browser.storage.local.set({settings: merged});
                await sleep(300);
            }
            for (let i = 0; i < 15 && !(keepass.isKeePassXCAvailable && keepass.databaseHash); i++) {
                await keepass.reconnect(null, 2000).catch(() => {});
                if (!(keepass.isKeePassXCAvailable && keepass.databaseHash)) await sleep(300);
            }
            if (!keepass.databaseHash) return {error: 'KeePassXC did not answer'};
            const ring = keepass.keyRing || {};
            const hash = keepass.databaseHash;
            if (!ring[hash] || ring[hash].id !== id || ring[hash].key !== key) keepass.saveKey(hash, id, key);
            await keepass.reconnect(null, 2000).catch(() => {});
            const stored = (await browser.storage.local.get('settings')).settings || {};
            return {associated: keepass.isAssociated(), settings: applied(stored) && applied(page.settings)};
        }"""
        with self.session() as (_, context):
            worker = None
            for _ in range(50):
                worker = next((w for w in context.service_workers if w.url.startswith("chrome-extension://%s/" % ext_id)),
                              None)
                if worker is not None:
                    break
                time.sleep(0.2)
            if worker is None:
                raise BrowserError("KeePassXC-Browser did not load in the bot's browser; see %s"
                                   % (self.dir / "chromium.log"))
            result = worker.evaluate(script, [identity["id"], identity["key"], EXTENSION_SETTINGS])
        if not (result or {}).get("associated"):
            raise BrowserError("the browser's KeePassXC extension could not connect (%s); check `alfred creds status`"
                               % ((result or {}).get("error") or "not associated"))
        if not result.get("settings"):
            raise BrowserError("the browser's KeePassXC extension did not keep its settings; try `alfred browser stop`, "
                               "then the command again")

    def wake_extension(self, context: Any) -> bool:
        """Make sure the extension is talking to KeePassXC, with the bot's identity accepted, before a page loads: it fills
        a form only when the page loads. Its worker can lose that when KeePassXC restarts (a credential was added),
        reloads a database (one was granted or withdrawn), or was not ready yet when the browser started. Whether it
        is ready."""
        ext = "chrome-extension://%s/" % extension_id(extension_dir(self.config))
        worker = next((w for w in context.service_workers if w.url.startswith(ext)), None)
        if worker is None:
            return False
        # Asking KeePassXC again matters: after it reloads a database (a credential granted or withdrawn) the extension
        # still looks connected but no longer associated, and would fill nothing on the next page.
        # Its per-tab cache goes too, as the extension itself does when a tab's URL changes (clearLogins): loading the
        # same URL again keeps it, so a withdrawn credential would still be filled in, and a granted one would not
        # (a repeated request for the same page and tab is answered with nothing).
        return bool(worker.evaluate("""async () => {
            for (const tab of await browser.tabs.query({})) await page.clearLogins(tab.id).catch(() => {});
            for (let i = 0; i < 3; i++) {
                if (!(keepass.isKeePassXCAvailable && keepass.databaseHash)) await keepass.reconnect(null, 2000).catch(() => {});
                if (await keepass.testAssociation(null, [true, false]).catch(() => false)) return true;
                await keepass.reconnect(null, 2000).catch(() => {});
            }
            return false;
        }"""))

    # -- driving it --

    @contextlib.contextmanager
    def session(self) -> Iterator[Tuple[Any, Any]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserError("Playwright is not installed: venv/bin/pip install -e '.[browser]'") from None
        state = self.state()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:%d" % int(state["port"]))
            yield browser, browser.contexts[0]

    @staticmethod
    def page_id(context: Any, page: Any) -> str:
        info = context.new_cdp_session(page).send("Target.getTargetInfo")
        return str(info["targetInfo"]["targetId"])[:8].lower()

    def pages(self, context: Any) -> List[Tuple[str, Any]]:
        return [(self.page_id(context, page), page) for page in context.pages
                if not page.url.startswith(("chrome-extension://", "devtools://"))]

    def find(self, context: Any, page_id: str) -> Any:
        wanted = (page_id or "").strip().lower()
        for found, page in self.pages(context):
            if found == wanted:
                return page
        raise BrowserError("no tab %r; `alfred browser tabs` lists them" % page_id)

    def tab_urls(self) -> List[str]:
        try:
            with self.session() as (_, context):
                return [page.url for _, page in self.pages(context) if page.url != "about:blank"]
        except Exception:  # noqa: BLE001 - only used to reopen tabs after a restart
            return []


# -- what a bot reads ----------------------------------------------------------


PASSWORD_VALUES = "() => Array.from(document.querySelectorAll('input[type=password]')).map(i => i.value).filter(v => v)"


def page_secrets(page: Any, granted: Sequence[str]) -> List[str]:
    """What must not leave a page: the bot's granted secrets, and whatever is typed in its password fields."""
    found = list(granted)
    for frame in page.frames:
        with contextlib.suppress(Exception):  # a frame that is gone or cross-origin and not ready
            found += frame.evaluate(PASSWORD_VALUES)
    return found


def check_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not REF_PATTERN.match(ref):
        raise BrowserError("not an element ref from `alfred browser snapshot`: %r (they look like e12)" % ref)
    return ref


def locate(page: Any, ref: str) -> Any:
    """An element by its snapshot ref. Refs belong to the snapshot that made them, and each command is a new
    connection, so the page is snapshotted again first: on a page that has not changed the refs are the same."""
    page.aria_snapshot(mode="ai")
    return page.locator("aria-ref=%s" % check_ref(ref))
