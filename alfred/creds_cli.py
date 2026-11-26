"""`alfred creds`: the credentials KeePassXC types into the bots' browsers (docs/CREDENTIALS.md).

On the machine, from a terminal (never from a bot's turn): `setup`, `add`, `remove`, `grant`, `revoke`, `start`,
`stop`. Anyone: `status`, and `list`, which in a bot's turn shows only what was granted to that bot. No command
ever prints a password or a TOTP seed.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Any, Dict, List

from .config import Config
from .keepass import CredentialError, KeePass

NOTIFY_SETTING = "ALFRED_CREDENTIALS_NOTIFY"


class CredsError(Exception):
    pass


def _in_bot_turn() -> bool:
    return bool(os.environ.get("ALFRED_BOT_ID", "").strip("0 ") or os.environ.get("ALFRED_BOT_KEY", "").strip())


def owner_only(action: str) -> None:
    """Credentials are added and given out only by the user on the machine. A bot's agent always has ALFRED_BOT_ID
    and ALFRED_BOT_KEY set; Claude Code's rules deny these subcommands to every bot as well."""
    if _in_bot_turn():
        raise CredsError("`alfred creds %s` is for the user, on the machine; a bot never runs it" % action)


def _secret(prompt: str, confirm: bool = True) -> str:
    if not sys.stdin.isatty():
        raise CredsError("type it at a terminal: the secret is read with no echo, never from an argument or a pipe")
    first = getpass.getpass(prompt)
    if confirm and getpass.getpass("Again: ") != first:
        raise CredsError("the two entries differ")
    return first


def _bot(config: Config, text: str):
    from .bots import BotRegistry

    record = BotRegistry(config).find(text)
    if record is None:
        raise CredsError("no bot %r" % text)
    return record


def notify(config: Config, record: Any, text: str) -> bool:
    """A line in the bot's chat with its owner, posted by the service as the bot. Never raises."""
    if config.get(NOTIFY_SETTING, "on").strip().lower() in ("off", "no", "false", "0"):
        return False
    try:
        from .scheduler import KIND_MESSAGE, SchedulerStore, utc_now

        SchedulerStore(config.db_path).add(chat_id=int(record.owner_id), due=utc_now(), text=text, kind=KIND_MESSAGE,
                                           bot_id=int(record.bot_id))
        return True
    except Exception:  # noqa: BLE001 - a notice never turns the change into a failure
        return False


def _audit(config: Config, event: str, record: Any, name: str) -> None:
    from .bots import audit

    audit(config, event, record, by="the user on the machine", via="alfred creds", credential=name)


def _bot_warnings(config: Config, record: Any) -> List[str]:
    notes = []
    if not record.allows("creds"):
        notes.append("%s does not have the creds permission yet: /bot perm %s creds on" % (record.handle, record.username))
    if not record.allows("browser"):
        notes.append("%s does not have the browser permission" % record.handle)
    from .bots import BotRegistry

    browser = BotRegistry(config).config_for(record.bot_id).browser
    if record.allows("browser") and browser != "bot":
        notes.append("%s uses %r: credentials are only filled in a bot's own browser (/bot set %s browser bot)"
                     % (record.handle, browser, record.username))
    return notes


def _handles(config: Config) -> Dict[int, str]:
    from .bots import BotRegistry

    return {record.bot_id: record.handle for record in BotRegistry(config).list()}


# -- the commands --------------------------------------------------------------


def cmd_setup(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    owner_only("setup")
    store = KeePass(config)
    done = store.setup()
    return {"ok": True, "done": done or ["already set up"], "directory": str(store.dir),
            "next": "alfred creds add <name> --url <site> --username <user>; then alfred creds grant <name> <bot>"}


def cmd_add(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    owner_only("add")
    store = KeePass(config)
    store.require_set_up()
    password = _secret("Password for %s: " % args.name)
    totp = _secret("TOTP key (the otpauth:// link from the QR code, or its base32 key): ", confirm=False) if args.totp else ""
    credential = store.add(args.name, args.url, args.username or "", password, totp, replace=args.replace)
    restarted = store.restart()  # the app opens databases only when it starts
    return {"ok": True, "credential": credential.describe(), "keepassxc_restarted": restarted}


def cmd_remove(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    owner_only("remove")
    store = KeePass(config)
    credential = store.remove(args.name)
    handles = _handles(config)
    from .bots import BotRegistry

    registry = BotRegistry(config)
    for bot_id in credential.bots:
        record = registry.get(bot_id)
        if record is not None:
            _audit(config, "credential_revoked", record, credential.name)
            notify(config, record, "🔑 The credential «%s» was deleted, so I can no longer log in with it." % credential.name)
    return {"ok": True, "removed": credential.name, "was_granted_to": [handles.get(b, str(b)) for b in credential.bots],
            "keepassxc_restarted": store.restart()}


def cmd_grant(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    owner_only("grant")
    record = _bot(config, args.bot)
    store = KeePass(config)
    changed = store.grant(args.name, record.bot_id)
    credential = store.get(args.name)
    if changed:
        _audit(config, "credential_granted", record, credential.name)
        notify(config, record, "🔑 You granted me the credential «%s» (%s). My browser will fill it in on those sites "
                               "by itself; I never see the password." % (credential.name, ", ".join(credential.urls)))
    return {"ok": True, "credential": credential.name, "bot": record.handle, "changed": changed,
            "notes": _bot_warnings(config, record)}


def cmd_revoke(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    owner_only("revoke")
    record = _bot(config, args.bot)
    store = KeePass(config)
    changed = store.revoke(args.name, record.bot_id)
    if changed:
        _audit(config, "credential_revoked", record, args.name)
        notify(config, record, "🔑 You withdrew the credential «%s» from me." % args.name)
    return {"ok": True, "credential": args.name, "bot": record.handle, "changed": changed,
            "note": "a session it already opened with it stays open until it expires or it logs out"}


def cmd_list(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    store = KeePass(config)
    if not store.is_set_up():
        return {"ok": True, "credentials": [], "note": "credentials are not set up (alfred creds setup)"}
    if _in_bot_turn():
        from .cli import require, verified_caller

        caller = verified_caller(config)
        if caller is None:
            raise CredsError("which bot? run it from a bot's turn")
        require(caller, "creds")
        mine = store.granted_to(caller.bot_id)
        return {"ok": True, "credentials": [{key: value for key, value in credential.describe().items() if key != "bots"}
                                            for credential in mine],
                "note": "your browser fills these in by itself on their sites; you never see or type the password"}
    handles = _handles(config)
    listed = []
    for credential in store.list():
        data = credential.describe()
        data["bots"] = [handles.get(bot_id, "%d (gone)" % bot_id) for bot_id in credential.bots]
        listed.append(data)
    return {"ok": True, "credentials": listed}


def cmd_status(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    store = KeePass(config)
    result: Dict[str, Any] = {"ok": True, "set_up": store.is_set_up(), "keepassxc_running": store.running()}
    if not _in_bot_turn():
        result["directory"] = str(store.dir)
        result["other_keepassxc"] = [line.split(" ", 1)[0] for line in store.other_instances()]
        result["credentials"] = len(store.names()) if store.is_set_up() else 0
    return result


def cmd_start(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    owner_only("start")
    started = KeePass(config).start()
    return {"ok": True, "started": started, "note": None if started else "it was already running"}


def cmd_stop(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
    owner_only("stop")
    return {"ok": True, "stopped": KeePass(config).stop()}


def run(handler):
    """CredentialError and CredsError become `{"ok": false, "error": ...}`, like every agent-facing command."""
    def wrapped(config: Config, args: argparse.Namespace) -> Dict[str, Any]:
        from .cli import CommandError

        try:
            return handler(config, args)
        except (CredentialError, CredsError) as exc:
            raise CommandError(str(exc)) from exc
        except KeyboardInterrupt:
            raise CommandError("cancelled") from None
    return wrapped


def add_parser(commands: Any) -> None:
    creds = commands.add_parser("creds", help="credentials the bots' browsers fill in, kept in KeePassXC")
    actions = creds.add_subparsers(dest="action", required=True)
    actions.add_parser("setup", help="create the store (on the machine)").set_defaults(json=run(cmd_setup))
    add = actions.add_parser("add", help="store a credential; asks for the password (on the machine)")
    add.add_argument("name", help="a short name, such as tum")
    add.add_argument("--url", action="append", required=True,
                     help="a site it may be typed into; repeat it for the sign-in page of a single sign-on")
    add.add_argument("--username", help="the user name or email")
    add.add_argument("--totp", action="store_true", help="also ask for the TOTP key (2FA codes)")
    add.add_argument("--replace", action="store_true", help="change an existing one; its grants stay")
    add.set_defaults(json=run(cmd_add))
    remove = actions.add_parser("remove", help="delete a credential (on the machine)")
    remove.add_argument("name")
    remove.set_defaults(json=run(cmd_remove))
    for verb, handler, text in (("grant", cmd_grant, "let a bot's browser use a credential (on the machine)"),
                                ("revoke", cmd_revoke, "take a credential back from a bot (on the machine)")):
        sub = actions.add_parser(verb, help=text)
        sub.add_argument("name")
        sub.add_argument("bot", help="@username, name or id")
        sub.set_defaults(json=run(handler))
    actions.add_parser("list", help="the credentials (in a bot's turn: only that bot's), never their secrets"
                       ).set_defaults(json=run(cmd_list))
    actions.add_parser("status", help="whether the store is set up and KeePassXC is running").set_defaults(json=run(cmd_status))
    actions.add_parser("start", help="open KeePassXC with the credentials (on the machine)").set_defaults(json=run(cmd_start))
    actions.add_parser("stop", help="close Consiglieri's KeePassXC (on the machine)").set_defaults(json=run(cmd_stop))
