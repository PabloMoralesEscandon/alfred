"""Each bot's personality: its own SOUL.md, and the base personality new bots start from.

A bot's personality lives in `SOUL.md` in its own directory, imported by its manual. It shapes tone and
manner only: what a bot may do comes from its permissions, which the panel sets and Claude Code enforces
outside the bot, and nothing written in SOUL.md can grant or lift any of them (the manual says so).

The base personality is one file in the panel's state (`state/soul-base.md`), editable from the panel;
until it is edited, it is the template shipped in `templates/SOUL.md`. A new bot starts with the base, or
with the personality written (or generated) for it in the creation assistant. Changing a bot's SOUL.md
reopens its Claude process at its next message, with the same conversation (see backends/claude.py).

Who writes a bot's SOUL.md: its owner, from the panel (My bots → a bot → Personality, or /bot soul). The
bot itself only with the `soul` permission, off in every preset; never another bot's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import Config
from .extensions import SECRET_PATTERNS

FILE_NAME = "SOUL.md"
BASE_FILE = "soul-base.md"  # in the panel's state directory
TEMPLATE = Path(__file__).resolve().parent / "templates" / "SOUL.md"
LIMIT = 4000  # characters


class SoulError(Exception):
    """Something the user can fix: an empty or too long personality, or one that looks like it holds a secret."""


def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def clean(text: str) -> str:
    """Check a personality before it is stored. Returns it with a single trailing newline."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        raise SoulError("the personality is empty")
    if len(text) > LIMIT:
        raise SoulError("the personality is too long: %d characters, at most %d" % (len(text), LIMIT))
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise SoulError("that looks like it holds a secret (%s); a personality never does" % label)
    return text + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.tmp" % path.name)
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# -- the base personality ------------------------------------------------------------


def base_path(config: Config) -> Path:
    return config.state_dir / BASE_FILE


def base(config: Config) -> str:
    """The base personality new bots start from: the edited one, or the template."""
    try:
        text = base_path(config).read_text(encoding="utf-8")
    except OSError:
        return template()
    return text if text.strip() else template()


def base_is_edited(config: Config) -> bool:
    return base_path(config).is_file()


def set_base(config: Config, text: str) -> str:
    cleaned = clean(text)
    _write(base_path(config), cleaned)
    return cleaned


def reset_base(config: Config) -> str:
    if base_path(config).exists():
        base_path(config).unlink()
    return template()


# -- one bot's personality -----------------------------------------------------------


def path_in(workspace: Path) -> Path:
    return Path(workspace) / FILE_NAME


def read(workspace: Path) -> Optional[str]:
    try:
        return path_in(workspace).read_text(encoding="utf-8")
    except OSError:
        return None


def ensure(config: Config, workspace: Path) -> bool:
    """Give a bot a SOUL.md from the base if it has none yet (a new bot, or one made before personalities).
    Returns whether it wrote one. Never replaces one that exists."""
    if path_in(workspace).exists():
        return False
    _write(path_in(workspace), clean(base(config)))
    return True


def write(workspace: Path, text: str) -> str:
    cleaned = clean(text)
    _write(path_in(workspace), cleaned)
    return cleaned


def reset(config: Config, workspace: Path) -> str:
    """Put the base personality back."""
    return write(workspace, base(config))
