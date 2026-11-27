"""Credentials for the bots, kept in KeePassXC (docs/CREDENTIALS.md).

The secrets never reach a bot's agent. KeePassXC holds them, and its browser extension (KeePassXC-Browser),
loaded in each bot's own browser (alfred/bot_browser.py), types them into login forms of the sites they
belong to. A bot only presses "Log in".

Which bot gets which credential is KeePassXC's own mechanism: an extension connects to KeePassXC with an
identity (a name and a key), and KeePassXC answers it only from databases that hold that identity. So:

    <home>/state/credentials/          0700, closed to every bot like the rest of state/
        master.key                     0400, the key file every database below opens with (no password)
        hub.kdbx                       empty; holds the identity of every bot with a browser. Always the
                                       active database, because the extension checks that one first
        <name>.kdbx                    one credential: one entry (user, password, URLs, TOTP seed), and the
                                       identities of the bots it is granted to
        identities.json                0600, each bot's identity {bot_id: {"id": "alfred-<id>", "key": ...}}
        keepassxc.ini                  KeePassXC's settings for this instance (never the user's own)

Granting is adding a bot's identity to a credential's database; revoking is taking it out. keepassxc-cli
has no command for that (nor for the TOTP field or extra URLs), so a database is rewritten whole: exported
as XML, changed, and imported again through a named pipe, so the plain XML never touches the disk.

The KeePassXC app must be running for the extension to fill anything. `start` opens it with every database
(the hub last, so it is the active one) and the key file; it stays open until the machine restarts.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid as uuid_module
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
HUB = "hub"
RESERVED = (HUB,)
IDENTITY_PREFIX = "alfred-"  # a bot's identity name: alfred-<bot_id>
BROWSER_KEY_PREFIX = "KPXC_BROWSER_"  # how KeePassXC stores an extension's identity in a database's metadata
EXTRA_URL_PREFIX = "KP2A_URL_"  # more URLs for one entry, which the browser integration also matches
MAC_APP = Path("/Applications/KeePassXC.app/Contents/MacOS")

# KeePassXC's settings for Consiglieri's instance. Nothing locks the databases (the bots work unattended),
# the browser integration is on and answers without asking, only the best-matching entries are offered, it
# searches every open database (each one answers only the identities it holds), and the request that lists
# every entry of a database is refused.
SETTINGS_INI = """[General]
ConfigVersion=2
UpdateCheckMessageShown=true
AutoReloadOnChange=true
RememberLastDatabases=false
OpenPreviousDatabasesOnStartup=false

[GUI]
CheckForUpdates=false
MinimizeOnStartup=true
MinimizeOnClose=true
ShowTrayIcon=true

[Security]
LockDatabaseIdle=false
LockDatabaseMinimize=false
LockDatabaseScreenLock=false
LockDatabaseOnUserSwitch=false
QuickUnlock=false

[Browser]
Enabled=true
AlwaysAllowAccess=true
AlwaysAllowUpdate=false
BestMatchOnly=true
SearchInAllDatabases=true
AllowGetDatabaseEntriesRequest=false
AllowExpiredCredentials=false
UnlockDatabase=false
UpdateBinaryPath=false
ShowNotification=false
"""


class CredentialError(Exception):
    """Something the user can fix: a bad name, an unknown credential, KeePassXC missing or not running."""


@dataclass
class Credential:
    """What may be said about a credential. Never its password or TOTP seed."""

    name: str
    username: str = ""
    urls: List[str] = field(default_factory=list)
    totp: bool = False
    bots: List[int] = field(default_factory=list)  # the bots it is granted to

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "username": self.username, "urls": list(self.urls), "totp": self.totp,
                "bots": list(self.bots)}


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_PATTERN.match(name) or name in RESERVED:
        raise CredentialError("a credential's name is a lower-case letter, then letters, digits, - or _ (up to 40), "
                              "and not %s: %r" % (", ".join(RESERVED), name))
    return name


def clean_url(url: str) -> str:
    """An https URL, or a bare host that becomes one. http only for localhost, which the tests use."""
    url = (url or "").strip()
    if not url:
        raise CredentialError("a URL is empty")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    try:
        parsed.port  # noqa: B018 - raises on a port that is not a number (javascript:alert(1) -> https://javascript:...)
    except ValueError:
        raise CredentialError("not a web address: %r" % url) from None
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise CredentialError("not a web address: %r" % url)
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise CredentialError("only https sites: %r" % url)
    return url


def check_totp(uri: str) -> str:
    """An otpauth://totp/ link (from the site's QR code) or a bare base32 key, as an otpauth link."""
    text = (uri or "").strip().replace(" ", "")
    if not text:
        raise CredentialError("the TOTP key is empty")
    if not text.lower().startswith("otpauth://"):
        text = "otpauth://totp/alfred?secret=%s&period=30&digits=6" % text.upper()
    totp_parts(text)  # raises if it can't be used
    return text


def totp_parts(uri: str) -> Dict[str, Any]:
    parsed = urlparse(uri)
    if parsed.scheme != "otpauth" or parsed.netloc.lower() != "totp":
        raise CredentialError("not an otpauth://totp/ link")
    query = {key.lower(): values[0] for key, values in parse_qs(parsed.query).items()}
    secret = unquote(query.get("secret", "")).upper().replace(" ", "")
    try:
        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    except (ValueError, TypeError):
        raise CredentialError("the TOTP secret is not base32") from None
    digits, period = int(query.get("digits", 6)), int(query.get("period", 30))
    algorithm = query.get("algorithm", "SHA1").upper()
    if not key or digits not in (6, 7, 8) or not 1 <= period <= 300 or algorithm not in ("SHA1", "SHA256", "SHA512"):
        raise CredentialError("unsupported TOTP settings (digits %s, period %s, %s)" % (digits, period, algorithm))
    return {"key": key, "digits": digits, "period": period, "algorithm": algorithm}


def totp_code(uri: str, at: Optional[float] = None) -> str:
    """RFC 6238. Consiglieri never shows it: it only knows the codes so it can hide them in what a bot reads."""
    parts = totp_parts(uri)
    counter = int((time.time() if at is None else at) // parts["period"])
    digest = hmac.new(parts["key"], struct.pack(">Q", counter), getattr(hashlib, parts["algorithm"].lower())).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** parts["digits"]
    return str(number).zfill(parts["digits"])


def _kdbx_time() -> str:
    """Now, as KeePass 4 writes times in XML: seconds since year 1, little-endian, base64."""
    seconds = int((datetime.datetime.utcnow() - datetime.datetime(1, 1, 1)).total_seconds())
    return base64.b64encode(seconds.to_bytes(8, "little")).decode()


def _uuid() -> str:
    return base64.b64encode(uuid_module.uuid4().bytes).decode()


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    _private_dir(path.parent)
    temporary = path.with_name(".%s.tmp" % path.name)
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _find(names: Sequence[str]) -> str:
    for name in names:
        if os.path.isabs(name):
            if os.access(name, os.X_OK):
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    return ""


class KeePass:
    """The credential store. Cheap to create; reads the files on every call.

    `run` stands in for subprocess.run and `popen` for subprocess.Popen, so tests can watch the calls."""

    def __init__(self, config: Config, run: Callable[..., Any] = subprocess.run,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.config = config
        self.dir = config.state_dir / "credentials"
        self.run = run
        self.popen = popen

    # -- where things are --

    @property
    def key_file(self) -> Path:
        return self.dir / "master.key"

    @property
    def hub(self) -> Path:
        return self.dir / ("%s.kdbx" % HUB)

    def path(self, name: str) -> Path:
        return self.dir / ("%s.kdbx" % check_name(name))

    @property
    def settings_file(self) -> Path:
        return self.dir / "keepassxc.ini"

    @property
    def pid_file(self) -> Path:
        return self.dir / "keepassxc.pid"

    def cli(self) -> str:
        found = _find([self.config.get("CONSIGLIERI_KEEPASSXC_CLI") or "", str(MAC_APP / "keepassxc-cli"), "keepassxc-cli"])
        if not found:
            raise CredentialError("keepassxc-cli is not installed: install KeePassXC (on macOS: brew install --cask "
                                  "keepassxc), or set CONSIGLIERI_KEEPASSXC_CLI")
        return found

    def app(self) -> str:
        found = _find([self.config.get("CONSIGLIERI_KEEPASSXC") or "", str(MAC_APP / "KeePassXC"), "keepassxc"])
        if not found:
            raise CredentialError("the KeePassXC app is not installed (on macOS: brew install --cask keepassxc), or set "
                                  "CONSIGLIERI_KEEPASSXC")
        return found

    def proxy(self) -> str:
        """The program a browser starts to talk to KeePassXC (native messaging)."""
        found = _find([self.config.get("CONSIGLIERI_KEEPASSXC_PROXY") or "", str(MAC_APP / "keepassxc-proxy"),
                       "keepassxc-proxy"])
        if not found:
            raise CredentialError("keepassxc-proxy is not installed with KeePassXC; set CONSIGLIERI_KEEPASSXC_PROXY")
        return found

    def is_set_up(self) -> bool:
        return self.key_file.is_file() and self.hub.is_file()

    def require_set_up(self) -> None:
        if not self.is_set_up():
            raise CredentialError("credentials are not set up; run `alfred creds setup` on the machine")

    # -- keepassxc-cli --

    def _cli(self, *args: str, stdin: str = "", check: bool = True) -> str:
        process = self.run([self.cli(), *args], input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=60, check=False)
        if check and process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip().splitlines()
            raise CredentialError("keepassxc-cli %s failed: %s" % (args[0], detail[-1] if detail else process.returncode))
        return process.stdout

    def _open_args(self) -> List[str]:
        return ["-q", "--no-password", "--key-file", str(self.key_file)]

    def export(self, path: Path) -> ET.Element:
        """A database as XML, in this process only (it holds the secrets in clear)."""
        return ET.fromstring(self._cli("export", *self._open_args(), "--format", "xml", str(path)))

    def write(self, path: Path, root: ET.Element) -> None:
        """Replace a database with this XML. The XML goes to keepassxc-cli through a named pipe in the private
        directory, never as a file; the new database replaces the old one in a single rename, which the open
        KeePassXC app notices and reloads."""
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        work = Path(tempfile.mkdtemp(prefix=".write-", dir=str(self.dir)))
        try:
            pipe, new = work / "in.xml", work / "new.kdbx"
            os.mkfifo(str(pipe), 0o600)
            errors: List[BaseException] = []

            def feed() -> None:
                try:
                    with open(str(pipe), "wb") as handle:
                        handle.write(data)
                except BaseException as exc:  # noqa: BLE001 - reported below
                    errors.append(exc)

            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
            try:
                self._cli("import", "-q", "--set-key-file", str(self.key_file), str(pipe), str(new))
            finally:
                if feeder.is_alive():  # keepassxc-cli failed before opening the pipe: let the writer go
                    try:
                        os.close(os.open(str(pipe), os.O_RDONLY | os.O_NONBLOCK))
                    except OSError:
                        pass
                feeder.join(5)
            if errors:
                raise CredentialError("could not write the database: %s" % errors[0])
            os.chmod(str(new), 0o600)
            os.replace(str(new), str(path))
        finally:
            shutil.rmtree(str(work), ignore_errors=True)

    # -- set up --

    def setup(self) -> List[str]:
        """Create the directory, the key file, the hub and KeePassXC's settings. Idempotent."""
        done = []
        _private_dir(self.dir)
        if not self.key_file.exists():
            if self.hub.exists():
                raise CredentialError("%s exists without its key file; move it away to start again" % self.hub)
            self._cli("db-create", "-q", "--set-key-file", str(self.key_file), str(self.hub))
            os.chmod(str(self.key_file), 0o400)
            os.chmod(str(self.hub), 0o600)
            done.append("created the key file and the hub database")
        elif not self.hub.exists():
            self.write(self.hub, self._new_database(HUB))
            done.append("created the hub database")
        if not self.settings_file.exists() or self.settings_file.read_text(encoding="utf-8") != SETTINGS_INI:
            _write_private(self.settings_file, SETTINGS_INI)
            done.append("wrote KeePassXC's settings")
        return done

    # -- databases as XML --

    def _new_database(self, name: str) -> ET.Element:
        root = ET.Element("KeePassFile")
        meta = ET.SubElement(root, "Meta")
        ET.SubElement(meta, "Generator").text = "Consiglieri"
        ET.SubElement(meta, "DatabaseName").text = name
        ET.SubElement(meta, "CustomData")
        group = ET.SubElement(ET.SubElement(root, "Root"), "Group")
        ET.SubElement(group, "UUID").text = _uuid()
        ET.SubElement(group, "Name").text = name
        return root

    @staticmethod
    def _custom_data(root: ET.Element) -> ET.Element:
        meta = root.find("Meta")
        if meta is None:
            meta = ET.SubElement(root, "Meta")
        data = meta.find("CustomData")
        if data is None:
            data = ET.SubElement(meta, "CustomData")
        return data

    @classmethod
    def _identities_in(cls, root: ET.Element) -> Dict[str, str]:
        found = {}
        for item in cls._custom_data(root).findall("Item"):
            key = item.findtext("Key") or ""
            if key.startswith(BROWSER_KEY_PREFIX):
                found[key[len(BROWSER_KEY_PREFIX):]] = item.findtext("Value") or ""
        return found

    @classmethod
    def _set_identity(cls, root: ET.Element, ident: str, key: Optional[str]) -> bool:
        """Add (key) or remove (None) one identity. Whether anything changed."""
        data = cls._custom_data(root)
        changed = False
        for item in list(data.findall("Item")):
            if item.findtext("Key") == BROWSER_KEY_PREFIX + ident:
                if key is not None and item.findtext("Value") == key:
                    return False
                data.remove(item)
                changed = True
        if key is not None:
            item = ET.SubElement(data, "Item")
            ET.SubElement(item, "Key").text = BROWSER_KEY_PREFIX + ident
            ET.SubElement(item, "Value").text = key
            ET.SubElement(item, "LastModificationTime").text = _kdbx_time()
            changed = True
        return changed

    @staticmethod
    def _entry(root: ET.Element) -> Optional[ET.Element]:
        group = root.find("Root/Group")
        return None if group is None else group.find("Entry")

    @staticmethod
    def _strings(entry: ET.Element) -> Dict[str, str]:
        return {item.findtext("Key") or "": item.findtext("Value") or "" for item in entry.findall("String")}

    # -- the credentials --

    def names(self) -> List[str]:
        if not self.dir.is_dir():
            return []
        return sorted(path.stem for path in self.dir.glob("*.kdbx") if path.stem not in RESERVED
                      and NAME_PATTERN.match(path.stem))

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def add(self, name: str, urls: Sequence[str], username: str, password: str, totp: str = "",
            replace: bool = False) -> Credential:
        """Store one credential in its own database. Replacing keeps its grants."""
        self.require_set_up()
        name = check_name(name)
        urls = [clean_url(url) for url in urls]
        if not urls:
            raise CredentialError("a credential needs at least one URL: the sites it may be typed into")
        if not password:
            raise CredentialError("the password is empty")
        totp = check_totp(totp) if totp else ""
        path = self.path(name)
        grants: Dict[str, str] = {}
        if path.exists():
            if not replace:
                raise CredentialError("there is already a credential %r; add --replace to change it" % name)
            grants = self._identities_in(self.export(path))
        root = self._new_database(name)
        for ident, key in grants.items():
            self._set_identity(root, ident, key)
        entry = ET.SubElement(root.find("Root/Group"), "Entry")
        ET.SubElement(entry, "UUID").text = _uuid()
        values = [("Title", name, False), ("UserName", username, False), ("Password", password, True),
                  ("URL", urls[0], False)]
        values += [("%s%d" % (EXTRA_URL_PREFIX, number), url, False) for number, url in enumerate(urls[1:], 1)]
        if totp:
            values.append(("otp", totp, True))
        for key, value, protect in values:
            string = ET.SubElement(entry, "String")
            ET.SubElement(string, "Key").text = key
            ET.SubElement(string, "Value", {"ProtectInMemory": "True"} if protect else {}).text = value
        self.write(path, root)
        return self.get(name)

    def remove(self, name: str) -> Credential:
        credential = self.get(name)
        self.path(name).unlink()
        return credential

    def get(self, name: str) -> Credential:
        self.require_set_up()
        path = self.path(name)
        if not path.is_file():
            raise CredentialError("there is no credential %r" % name)
        return self._describe(name, self.export(path))

    def _describe(self, name: str, root: ET.Element) -> Credential:
        credential = Credential(name=name)
        entry = self._entry(root)
        if entry is not None:
            strings = self._strings(entry)
            credential.username = strings.get("UserName", "")
            credential.urls = [strings["URL"]] if strings.get("URL") else []
            credential.urls += [value for key, value in sorted(strings.items())
                                if key.startswith(EXTRA_URL_PREFIX) and value]
            credential.totp = bool(strings.get("otp") or strings.get("TOTP Seed"))
        credential.bots = sorted(bot_id for bot_id in (bot_of(ident) for ident in self._identities_in(root)) if bot_id)
        return credential

    def list(self) -> List[Credential]:
        return [self.get(name) for name in self.names()]

    def granted_to(self, bot_id: int) -> List[Credential]:
        return [credential for credential in self.list() if int(bot_id) in credential.bots]

    def secrets_for(self, bot_id: int) -> List[str]:
        """The passwords and current TOTP codes of a bot's credentials: only so that whatever the bot reads from
        its browser can be scrubbed of them (alfred/bot_browser.py)."""
        found: List[str] = []
        ident = identity_name(bot_id)
        for name in self.names():
            root = self.export(self.path(name))
            entry = self._entry(root)
            if entry is None or ident not in self._identities_in(root):
                continue
            strings = self._strings(entry)
            if strings.get("Password"):
                found.append(strings["Password"])
            if strings.get("otp"):
                now = time.time()
                try:
                    found += [totp_code(strings["otp"], now + step * 30) for step in (-1, 0, 1)]
                except CredentialError:
                    pass
        return found

    # -- the bots' identities --

    def _identities_file(self) -> Path:
        return self.dir / "identities.json"

    def _identities(self) -> Dict[str, Dict[str, str]]:
        try:
            data = json.loads(self._identities_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def identity(self, bot_id: int, create: bool = True) -> Optional[Dict[str, str]]:
        """A bot's identity for KeePassXC: its name and a random key, made once."""
        identities = self._identities()
        found = identities.get(str(int(bot_id)))
        if found or not create:
            return found
        self.require_set_up()
        found = {"id": identity_name(bot_id), "key": base64.b64encode(secrets.token_bytes(32)).decode()}
        identities[str(int(bot_id))] = found
        _write_private(self._identities_file(), json.dumps(identities, indent=2, sort_keys=True) + "\n")
        return found

    def _change(self, path: Path, ident: str, key: Optional[str]) -> bool:
        root = self.export(path)
        if not self._set_identity(root, ident, key):
            return False
        self.write(path, root)
        return True

    def connect(self, bot_id: int) -> Dict[str, str]:
        """Put a bot's identity in the hub, so its browser's extension may talk to KeePassXC at all (it still gets
        nothing until a credential is granted to it)."""
        self.require_set_up()
        found = self.identity(bot_id)
        self._change(self.hub, found["id"], found["key"])
        return found

    def grant(self, name: str, bot_id: int) -> bool:
        """Whether it was not granted already."""
        self.get(name)
        found = self.identity(bot_id)
        self._change(self.hub, found["id"], found["key"])
        return self._change(self.path(name), found["id"], found["key"])

    def revoke(self, name: str, bot_id: int) -> bool:
        """Whether it was granted."""
        self.get(name)
        return self._change(self.path(name), identity_name(bot_id), None)

    def forget_bot(self, bot_id: int) -> List[str]:
        """A destroyed bot: out of every database, and its identity gone. Returns the credentials it had."""
        if not self.is_set_up():
            return []
        ident = identity_name(bot_id)
        had = []
        for name in self.names():
            if self._change(self.path(name), ident, None):
                had.append(name)
        self._change(self.hub, ident, None)
        identities = self._identities()
        if identities.pop(str(int(bot_id)), None) is not None:
            _write_private(self._identities_file(), json.dumps(identities, indent=2, sort_keys=True) + "\n")
        return had

    # -- the KeePassXC app --

    def _pid(self) -> int:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _command_of(self, pid: int) -> str:
        if pid <= 0:
            return ""
        process = self.run(["ps", "-p", str(pid), "-o", "command="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, check=False)
        return (process.stdout or "").strip()

    def running(self) -> bool:
        return str(self.settings_file) in self._command_of(self._pid())

    def other_instances(self) -> List[str]:
        """KeePassXC apps of this user that are not Consiglieri's. One app serves the browser per user: another one
        would get Consiglieri's databases handed to it, or answer the bots' browsers instead."""
        process = self.run(["ps", "-x", "-o", "pid=,command="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, check=False)
        mine = self._pid() if self.running() else 0
        others = []
        for line in (process.stdout or "").splitlines():
            pid, _, command = line.strip().partition(" ")
            if pid.isdigit() and int(pid) != mine and re.search(r"(^|/)(KeePassXC|keepassxc)( |$)", command.strip()):
                others.append(line.strip())
        return others

    def databases(self) -> List[Path]:
        """What the app opens, in order: every credential, then the hub, which must end up the active one."""
        return [self.path(name) for name in self.names()] + [self.hub]

    def start(self) -> bool:
        """Open KeePassXC with every database, if it is not running. Whether it was started now."""
        self.require_set_up()
        self.setup()  # the settings file is current
        if self.running():
            return False
        others = self.other_instances()
        if others:
            raise CredentialError("another KeePassXC is open (%s); quit it first: only one can serve the browsers, "
                                  "and Consiglieri keeps its own" % others[0].split(" ", 1)[0])
        databases = self.databases()
        log = open(str(self.dir / "keepassxc.log"), "ab")
        try:
            process = self.popen(
                [self.app(), "--config", str(self.settings_file), "--localconfig", str(self.dir / "keepassxc-local.ini"),
                 "--keyfile", str(self.key_file), "--pw-stdin", *[str(path) for path in databases]],
                stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True,
            )
            process.stdin.write(b"\n" * len(databases))  # no passwords: the key file opens them
            process.stdin.close()
        finally:
            log.close()
        _write_private(self.pid_file, "%d\n" % process.pid)
        return True

    def stop(self) -> bool:
        """Close Consiglieri's KeePassXC. Whether it was running."""
        if not self.running():
            return False
        pid = self._pid()
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        for _ in range(50):
            if not self.running():
                break
            time.sleep(0.1)
        return True

    def restart(self) -> bool:
        """After a credential is added or removed: the app only opens databases when it starts. Whether it runs."""
        if not self.running():
            return False
        self.stop()
        self.start()
        return True


def identity_name(bot_id: int) -> str:
    return "%s%d" % (IDENTITY_PREFIX, int(bot_id))


def bot_of(ident: str) -> int:
    """The bot an identity name stands for, or 0 for one Consiglieri did not make."""
    if ident.startswith(IDENTITY_PREFIX) and ident[len(IDENTITY_PREFIX):].isdigit():
        return int(ident[len(IDENTITY_PREFIX):])
    return 0


def platform_supported() -> bool:
    """Named pipes and the KeePassXC app: macOS and Linux."""
    return sys.platform != "win32"
