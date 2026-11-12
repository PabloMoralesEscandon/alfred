"""Security presets for the bots: a few named sets of permissions, in plain words, as data.

A preset is a map of permission -> value over `bots.DEFAULT_PERMISSIONS`: anything a preset does not
name keeps its default. So a permission added to `bots.PERMISSIONS` later is in every preset at its
default, and putting it in one preset is one line in `PRESET_PERMISSIONS`. A new preset is one entry
there and one in `PRESET_INFO`; the order of `PRESET_PERMISSIONS` is the order from least to most.

Nothing about a preset is stored on a bot. A bot keeps its permissions as before, and its preset is
worked out when it is read: the preset whose values it has, or "Custom" based on the closest one. So
bots made before presets existed keep working unchanged, and so does a bot whose switches were moved.

The free-form permissions (`FREEFORM`: extra folders and extra rules) belong to the bot, not to a
preset: choosing a preset keeps them, and they do not count when matching a bot to a preset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .bots import DEFAULT_PERMISSIONS, PERMISSION_NAMES, SWITCHES

# preset -> the permissions it sets, over the defaults. From least to most.
PRESET_PERMISSIONS: Dict[str, Dict[str, str]] = {
    "basic": {},
    "assistant": {"web": "on", "edit": "on"},
    "power": {"web": "on", "edit": "on", "browser": "on", "shell": "full", "sandbox": "on", "mode": "acceptEdits"},
    "full": {"web": "on", "edit": "on", "browser": "on", "mcp": "on", "contact": "on", "bots": "on",
             "shell": "full", "sandbox": "off", "mode": "bypassPermissions"},
}
RECOMMENDED = "assistant"  # what the creation assistant starts with
# Levels above Full access that are not a set of permissions: level -> the BotRecord flag that holds it.
# Cross-agent access is one: it lives beside the permissions (BotRecord.cross_agent), is granted only to a
# bot that exists, from the control panel, after a warning, and is audited (BotPool.set_cross_agent). So
# choosing a preset never changes it, it can sit on top of any preset, and `find` never names it.
SPECIAL_LEVELS: Dict[str, str] = {"cross": "cross_agent"}
FREEFORM = ("folders", "allow", "deny")


@dataclass(frozen=True)
class PresetInfo:
    icon: str
    label: str
    can: str
    cannot: str
    warning: str = ""


PRESET_INFO: Dict[str, PresetInfo] = {
    "basic": PresetInfo(
        "🔒", "Basic",
        can="talk with you, set reminders, keep notes and files in its own folder",
        cannot="go online, touch your other files, run commands, use your browser or message anyone else",
    ),
    "assistant": PresetInfo(
        "⭐", "Assistant",
        can="everything Basic can, plus search the web, read web pages and edit files in its work folder",
        cannot="run commands, use your browser or your logins, use your connectors or message anyone else",
    ),
    "power": PresetInfo(
        "🛠", "Power",
        can="everything Assistant can, plus use your browser and run any command inside a sandbox "
            "(it can only write in its folders)",
        cannot="run commands outside the sandbox, use your connectors, message anyone else or manage other bots",
        warning="⚠️ It uses the browser where you are logged in, so it can act on those accounts.",
    ),
    "full": PresetInfo(
        "🔓", "Full access",
        can="anything your user can do on this Mac, without asking: any command with no sandbox, your browser "
            "and logins, your connectors, other chats and other bots",
        cannot="almost nothing: with an unsandboxed shell the remaining rules can be got around",
        warning="⚠️⚠️ No checks at all. It can delete files, spend from your accounts and write to anyone. "
                "Only for a bot you trust completely, ideally on a separate machine.",
    ),
    "cross": PresetInfo(
        "⚠️", "Cross-agent access",
        can="on top of its preset, read the memory of your other bots and use their tools "
            "(skills, extensions, scripts)",
        cannot="change other agents' memory or tools, or open their tokens, conversations and inboxes. Below "
               "this level, even with Full access, no bot can read other agents' memory or use their tools",
        warning="⚠️⚠️ Above Full access. Only for a bot that already exists and that you trust with everything; "
                "granted from My bots → Permissions after a warning, and logged (/bot audit).",
    ),
}

# Other words `/bot perm <bot> <preset>` accepts, including the names the presets had before.
ALIASES: Dict[str, str] = {
    "minimal": "basic", "minimo": "basic", "mínimo": "basic", "basico": "basic", "básico": "basic",
    "standard": "assistant", "estandar": "assistant", "estándar": "assistant", "recommended": "assistant",
    "asistente": "assistant",
    "broad": "power", "amplio": "power",
    "full-access": "full", "fullaccess": "full", "completo": "full", "total": "full",
}

# How permissions read in a list of changes. A permission missing here reads as "name: value".
WORDS: Dict[str, str] = {
    "edit": "file edits", "web": "web", "browser": "browser", "mcp": "connectors (MCP)",
    "schedule": "reminders", "contact": "other chats", "bots": "other bots", "sandbox": "sandbox",
    "shared": "shared-memory writes",
    "soul": "own role and personality edits",
    "usage": "plan usage",
}
# Permission modes from the tightest to the loosest, for telling whether a change grants more.
MODE_RISK = {"dontAsk": 0, "plan": 0, "default": 0, "acceptEdits": 1, "auto": 2, "bypassPermissions": 3}
# The value of each permission that grants more. Switches not listed: "on".
LOOSER = {"sandbox": "off", "shell": "full"}

CUSTOM_ICON, CUSTOM_LABEL = "🎛", "Custom"


def keys() -> List[str]:
    """The presets that are sets of permissions, from least to most."""
    return list(PRESET_PERMISSIONS)


def levels() -> List[str]:
    """Every level, from least to most: the presets, then the special levels above them."""
    return keys() + list(SPECIAL_LEVELS)


def special_levels(record: object) -> List[str]:
    """The special levels a bot holds."""
    return [key for key, flag in SPECIAL_LEVELS.items() if getattr(record, flag, False) is True]


def managed() -> List[str]:
    """The permissions a preset decides: all of them but the free-form ones."""
    return [name for name in PERMISSION_NAMES if name not in FREEFORM]


def _listed() -> List[str]:
    """The managed permissions in the order a list of changes names them: the switches, then the rest."""
    return [name for name in managed() if name in SWITCHES] + [name for name in managed() if name not in SWITCHES]


def values(permissions: Mapping[str, str]) -> Dict[str, str]:
    """Every managed permission's value, from a bot's stored permissions (only what differs from the defaults)."""
    return {name: permissions.get(name) or DEFAULT_PERMISSIONS[name] for name in managed()}


def resolve(key: str) -> Dict[str, str]:
    return values(PRESET_PERMISSIONS[key])


def find(text: str) -> Optional[str]:
    """The preset a word names (its key, its label or an alias), or None."""
    word = (text or "").strip().lower()
    if word in PRESET_PERMISSIONS:
        return word
    for key in keys():  # never a special level: those are granted their own way
        if word == PRESET_INFO[key].label.lower():
            return key
    return ALIASES.get(word)


def apply(key: str, permissions: Mapping[str, str]) -> Dict[str, str]:
    """A bot's permissions with a preset chosen: the preset's values, the bot's own free-form ones kept."""
    chosen = {name: value for name, value in permissions.items() if name in FREEFORM}
    chosen.update(resolve(key))
    return {name: value for name, value in chosen.items() if value != DEFAULT_PERMISSIONS.get(name)}


def detect(permissions: Mapping[str, str]) -> Tuple[Optional[str], str]:
    """(the preset these permissions are, or None for custom; the closest preset, the base of a custom set).

    Closest means the fewest permissions that differ; on a tie, the one with less.
    """
    current = values(permissions)
    distances = []
    for index, key in enumerate(PRESET_PERMISSIONS):
        target = resolve(key)
        distances.append((sum(1 for name in current if current[name] != target[name]), index, key))
    distance, _, closest = min(distances)
    return (closest if distance == 0 else None), closest


def phrase(name: str, value: str) -> str:
    if name in SWITCHES:
        return "%s %s" % ("+" if value == "on" else "−", WORDS.get(name, name))
    if name == "shell":
        return "shell: %s" % ("any command" if value == "full" else "only alfred")
    return "%s: %s" % (WORDS.get(name, name), value)


def differences(base: str, permissions: Mapping[str, str]) -> List[str]:
    """What these permissions change from a preset, in short words: ["+ browser", "− web"]."""
    current, target = values(permissions), resolve(base)
    return [phrase(name, current[name]) for name in _listed() if current[name] != target[name]]


def looser(name: str, old: str, new: str) -> bool:
    """Whether changing one permission from old to new grants more."""
    if old == new:
        return False
    if name == "mode":
        return MODE_RISK.get(new, 3) > MODE_RISK.get(old, 3)
    if name in LOOSER:
        return new == LOOSER[name]
    if name in SWITCHES:
        return new == "on"
    return True  # a permission this module does not know: treat any change as more, so it is confirmed


def gains(current: Mapping[str, str], target: Mapping[str, str]) -> List[str]:
    """What going from one set of permissions to another newly allows. Empty when it only takes away."""
    old, new = values(current), values(target)
    return [phrase(name, new[name]) for name in _listed() if looser(name, old[name], new[name])]


def title(key: str, recommended_mark: bool = False) -> str:
    info = PRESET_INFO[key]
    suffix = " (recommended)" if recommended_mark and key == RECOMMENDED else ""
    return "%s %s%s" % (info.icon, info.label, suffix)


def card(key: str) -> str:
    """A preset in a few lines: what it can and cannot do, and its warning."""
    info = PRESET_INFO[key]
    lines = [title(key, recommended_mark=True), "Can: %s." % info.can, "Cannot: %s." % info.cannot]
    if info.warning:
        lines.append(info.warning)
    return "\n".join(lines)


def name_of(permissions: Mapping[str, str], base: Optional[str] = None) -> str:
    """"⭐ Assistant", or "🎛 Custom (from ⭐ Assistant: + browser)" for a set that matches no preset.

    `base` is the preset the set was started from, when known (the creation assistant knows it);
    otherwise the closest preset.
    """
    exact, closest = detect(permissions)
    if exact is not None:
        return title(exact)
    base = base if base in PRESET_PERMISSIONS else closest
    changes = differences(base, permissions)
    return "%s %s (from %s: %s)" % (CUSTOM_ICON, CUSTOM_LABEL, title(base), ", ".join(changes))


def level_name(record: object) -> str:
    """A bot's whole level: its preset (or Custom), plus any special level it holds on top."""
    name = name_of(getattr(record, "permissions", {}) or {})
    return " + ".join([name] + [title(key) for key in special_levels(record)])
