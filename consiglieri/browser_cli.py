"""`alfred browser`: a bot drives its own browser (alfred/bot_browser.py, docs/CREDENTIALS.md).

In a bot's turn it is that bot's browser, checked against ALFRED_BOT_KEY; from a terminal, `--bot` says whose
(the user opening it to log in by hand, for instance). Every command answers JSON. What comes back of a page is
scrubbed: no password, no TOTP code of the bot's credentials, no value of a password field.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bot_browser import BotBrowser, BrowserError, install, locate, page_secrets, scrub
from .config import Config
from .keepass import CredentialError, KeePass

TEXT_LIMIT = 20000
WAIT_SECONDS = 15


def _target(config: Config, args: argparse.Namespace):
    """The bot whose browser this is: the caller in its turn, else --bot from a terminal. Its browser is its own
    setting, else Consiglieri's."""
    from .cli import CommandError, require, verified_caller

    caller = verified_caller(config)
    if caller is not None:
        if getattr(args, "bot", None) and args.bot.lstrip("@").lower() not in (caller.username.lower(), str(caller.bot_id)):
            raise CommandError("%s can only use its own browser" % caller.handle)
        require(caller, "browser")
        record = caller
    else:
        if not getattr(args, "bot", None):
            raise CommandError("whose browser? pass --bot <@username|id>")
        from .bots import BotRegistry

        record = BotRegistry(config).find(args.bot)
        if record is None:
            raise CommandError("no bot %r" % args.bot)
    from .bots import BotRegistry

    browser = BotRegistry(config).config_for(record.bot_id).browser
    if browser != "bot":
        raise CommandError("%s uses %s, not a browser of its own (/bot set %s browser bot, or ALFRED_BROWSER=bot for "
                           "every bot); see the alfred:browser skill"
                           % (record.handle, {"orca": "Orca's browser", "chrome": "the Chrome profile",
                                              "none": "no browser"}.get(browser, browser), record.username))
    return record


def _creds(config: Config, record: Any) -> bool:
    return record.allows("creds") and KeePass(config).is_set_up()


class Driver:
    """One command: the bot's browser, running as its permissions say now, and the secrets to hide from it."""

    def __init__(self, config: Config, args: argparse.Namespace, headless: Optional[bool] = None) -> None:
        from .bots import BotRegistry

        self.record = _target(config, args)
        self.config = BotRegistry(config).config_for(self.record.bot_id)  # its own directory: outbox/ for screenshots
        config = self.config
        self.browser = BotBrowser(config, self.record.bot_id)
        self.creds = _creds(config, self.record)
        self.state = self.browser.ensure(self.creds, headless)
        self.granted: List[str] = KeePass(config).secrets_for(self.record.bot_id) if self.creds else []

    def ready(self, context: Any) -> None:
        """With credentials, the extension connected to KeePassXC before the command loads or touches a page."""
        if self.creds:
            self.browser.wake_extension(context)

    def clean(self, page: Any, text: str) -> str:
        return scrub(text or "", page_secrets(page, self.granted))

    def where(self, context: Any, page: Any) -> Dict[str, Any]:
        return {"page": self.browser.page_id(context, page), "url": scrub(page.url, self.granted),
                "title": self.clean(page, page.title())}


def _driven(handler):
    def wrapped(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
        from .cli import CommandError

        try:
            return handler(config, args)
        except (BrowserError, CredentialError) as exc:
            raise CommandError(str(exc)) from exc
        except CommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - Playwright's errors: the first line is the useful part
            message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            raise CommandError(message[:500]) from exc
    return wrapped


def _page_command(action):
    """A command on one tab: find it, act, and say where the tab is now."""
    @_driven
    def handler(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
        driver = Driver(config, args)
        with driver.browser.session() as (_, context):
            page = driver.browser.find(context, args.page)
            page.bring_to_front()  # the extension fills only the tab in front
            driver.ready(context)
            extra = action(driver, page, args) or {}
            result = {"ok": True}
            result.update(driver.where(context, page))
            result.update(extra)
            return result
    return handler


# -- the commands --------------------------------------------------------------


@_driven
def cmd_install(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .cli import CommandError

    if os.environ.get("ALFRED_BOT_ID", "").strip("0 "):
        raise CommandError("`alfred browser install` is for the user, on the machine")
    return {"ok": True, "done": install(config)}


@_driven
def cmd_status(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    record = _target(config, args)
    browser = BotBrowser(config, record.bot_id)
    result = {"ok": True, "bot": record.handle, "credentials_permission": record.allows("creds")}
    result.update(browser.status())
    return result


@_driven
def cmd_start(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    driver = Driver(config, args)
    return {"ok": True, "headless": driver.state.get("headless"), "credentials": driver.creds}


@_driven
def cmd_show(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """A visible window, for the user to log in by hand (over screen sharing, for instance). Tabs are reopened."""
    driver = Driver(config, args, headless=False)
    return {"ok": True, "headless": False,
            "note": "the browser window is open on the machine; `alfred browser hide` when the user is done"}


@_driven
def cmd_hide(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    Driver(config, args, headless=True)
    return {"ok": True, "headless": True}


@_driven
def cmd_stop(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    record = _target(config, args)
    return {"ok": True, "stopped": BotBrowser(config, record.bot_id).stop()}


@_driven
def cmd_open(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    driver = Driver(config, args)
    with driver.browser.session() as (_, context):
        driver.ready(context)
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        result = {"ok": True}
        result.update(driver.where(context, page))
        return result


@_driven
def cmd_tabs(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    driver = Driver(config, args)
    with driver.browser.session() as (_, context):
        return {"ok": True, "tabs": [driver.where(context, page) for _, page in driver.browser.pages(context)]}


def _goto(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    page.goto(args.url, wait_until="domcontentloaded")


def _snapshot(driver: Driver, page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    return {"snapshot": driver.clean(page, page.aria_snapshot(mode="ai"))}


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:  # noqa: BLE001 - a click that loads nothing is fine
        pass


def _click(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    locate(page, args.ref).click(timeout=WAIT_SECONDS * 1000)
    _settle(page)


def _fill(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    locate(page, args.ref).fill(args.text, timeout=WAIT_SECONDS * 1000)


def _type(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    page.keyboard.type(args.text)


def _press(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    page.keyboard.press(args.key)
    _settle(page)


def _select(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    locate(page, args.ref).select_option(label=args.value, timeout=WAIT_SECONDS * 1000)


def _check(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    element = locate(page, args.ref)
    (element.uncheck if args.off else element.check)(timeout=WAIT_SECONDS * 1000)


def _wait(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    timeout = (args.timeout or WAIT_SECONDS) * 1000
    if args.text:
        page.get_by_text(args.text).first.wait_for(timeout=timeout)
    elif args.url:
        page.wait_for_url("**%s**" % args.url, timeout=timeout)
    else:
        page.wait_for_load_state(args.load or "load", timeout=timeout)


def _text(driver: Driver, page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    text = driver.clean(page, page.inner_text("body"))
    return {"text": text[:TEXT_LIMIT], "truncated": len(text) > TEXT_LIMIT}


def _screenshot(driver: Driver, page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    default = driver.config.workspace / "outbox" / ("screenshot-%s.png" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    target = Path(args.out).expanduser() if args.out else default
    target.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target), full_page=args.full, mask=secret_fields(driver, page))
    return {"path": str(target)}


def secret_fields(driver: Driver, page: Any) -> List[Any]:
    """The fields a screenshot covers: every password field, and any other holding one of the bot's secrets (the
    TOTP code shows in a plain text field). Compared here, so the secrets never go into the page."""
    covered = [page.locator("input[type=password]")]
    if driver.granted:
        secrets = set(driver.granted)
        for field in page.locator("input, textarea").all():
            try:
                if field.input_value(timeout=1000) in secrets:
                    covered.append(field)
            except Exception:  # noqa: BLE001 - a field that went away
                continue
    return covered


def _back(driver: Driver, page: Any, args: argparse.Namespace) -> None:
    page.go_back(wait_until="domcontentloaded")


@_driven
def cmd_close(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    driver = Driver(config, args)
    with driver.browser.session() as (_, context):
        driver.browser.find(context, args.page).close()
    return {"ok": True, "closed": args.page}


def add_parser(commands: Any) -> None:
    browser = commands.add_parser("browser", help="a bot's own browser (ALFRED_BROWSER=bot)")
    actions = browser.add_subparsers(dest="action", required=True)

    def sub(name: str, help_text: str, handler, page: bool = False):
        parser = actions.add_parser(name, help=help_text)
        parser.add_argument("--bot", help="whose browser, from a terminal (in a bot's turn it is that bot's)")
        if page:
            parser.add_argument("--page", required=True, help="the tab, from `open` or `tabs`")
        parser.set_defaults(json=handler)
        return parser

    actions.add_parser("install", help="install Chromium and KeePassXC-Browser for the bots (on the machine)"
                       ).set_defaults(json=cmd_install)
    sub("status", "whether its browser is running", cmd_status)
    sub("start", "start it (commands start it anyway)", cmd_start)
    sub("show", "open it in a visible window, for the user to log in by hand", cmd_show)
    sub("hide", "back to no window", cmd_hide)
    sub("stop", "close it", cmd_stop)
    sub("open", "open a new tab", cmd_open).add_argument("url")
    sub("tabs", "its tabs", cmd_tabs)
    sub("goto", "load a URL in a tab", _page_command(_goto), page=True).add_argument("url")
    sub("snapshot", "the page as an accessibility tree with refs (e12) to act on", _page_command(_snapshot), page=True)
    sub("click", "click an element", _page_command(_click), page=True).add_argument("ref")
    fill = sub("fill", "clear a field and type into it", _page_command(_fill), page=True)
    fill.add_argument("ref")
    fill.add_argument("text")
    sub("type", "type at the current focus", _page_command(_type), page=True).add_argument("text")
    sub("press", "press a key, such as Enter or Tab", _page_command(_press), page=True).add_argument("key")
    select = sub("select", "choose an option of a list by its label", _page_command(_select), page=True)
    select.add_argument("ref")
    select.add_argument("value")
    check = sub("check", "tick a box (--off to untick)", _page_command(_check), page=True)
    check.add_argument("ref")
    check.add_argument("--off", action="store_true")
    wait = sub("wait", "wait for a text, a URL part or the page load", _page_command(_wait), page=True)
    group = wait.add_mutually_exclusive_group()
    group.add_argument("--text")
    group.add_argument("--url")
    group.add_argument("--load", choices=("load", "domcontentloaded", "networkidle"))
    wait.add_argument("--timeout", type=float, help="seconds (default %d)" % WAIT_SECONDS)
    sub("text", "the page's text", _page_command(_text), page=True)
    shot = sub("screenshot", "save a screenshot to a file (default: outbox/)", _page_command(_screenshot), page=True)
    shot.add_argument("--full", action="store_true")
    shot.add_argument("--out")
    sub("back", "go back", _page_command(_back), page=True)
    sub("close", "close a tab", cmd_close, page=True)
