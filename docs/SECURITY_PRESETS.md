# Security presets for bots

What a bot may do is offered as one of a few named presets instead of a dozen switches. The switches are
still there, behind **Customize**. This document lists the presets, where they live, how the menus and
`/bot perm` use them, and how to extend them.

## The presets

From least to most. Each is a set of permissions over the defaults in `bots.PERMISSIONS`; anything a preset
does not name keeps its default.

| Preset | Can | Cannot | Permissions it sets |
| --- | --- | --- | --- |
| 🔒 **Basic** | Talk with you, set reminders, keep notes and files in its own folder, check the plan usage when you allow it | Go online, touch your other files, run commands, use your browser, message anyone else | none (the defaults) |
| ⭐ **Assistant** (recommended) | Basic, plus search the web, read web pages, edit files in its work folder | Run commands, use your browser or logins, use your connectors, message anyone else | `web on`, `edit on` |
| 🛠 **Power** ⚠️ | Assistant, plus your browser and any command inside Claude Code's sandbox (writes only in its folders) | Run commands outside the sandbox, use connectors, message others, manage other bots | also `browser on`, `shell full`, `sandbox on`, `mode acceptEdits` |
| 🔓 **Full access** ⚠️⚠️ | Anything your user can do on this Mac, without asking | Almost nothing: an unsandboxed shell gets around the remaining rules | everything `on`, `shell full`, `sandbox off`, `mode bypassPermissions` |

Above the four presets is ⚠️ **Cross-agent access** (from the security-control work): on top of its
preset, the bot may read the memory of its owner's other bots and use their tools (never another user's bots, and
never Consiglieri's home, which also holds what an old home kept of the former main agent). It is not a set of
permissions. It is a flag on the bot (`BotRecord.cross_agent`), granted only to a bot that exists, only from
the control panel, after a warning, and every grant and withdrawal goes to the audit log (`/bot audit`). It can
sit on top of any preset, and choosing a preset never changes it.

## The plan usage tool: on in every preset, used only with your express permission

The `usage` permission (`alfred usage check`, `alfred usage running_low`, `alfred usage-alert`,
`alfred schedule add --skip-if-low` and the other usage conditions, and the `alfred:usage` skill) is **on by
default**, like `schedule` (reminders), so it is in every preset, Basic included. Having the tool is not leave
to use it: **a bot uses it only with its user's express permission**, that is, when the user asks it to check
the usage, gives it a rule about usage, or says yes when the bot asks. Without that it does not run the
commands and asks first. This rule is written in three places the bot reads or that describe it:

- the bot's manual: `bots.permission_summary()` adds `bots.USAGE_CONSENT_RULE` under "What you may do", and
  `templates/BOT.md` repeats it next to the `alfred` tools;
- the `alfred:usage` skill, in its description and in its first section (and the `alfred:reminders` skill,
  where it mentions usage conditions);
- this document and the permission table in the README.

The rule is an instruction, not a lock: nothing in Claude Code or in `alfred` can tell whether the user said
yes. The lock is the switch: `/bot perm <bot> usage off` (or **Customize**, or `-usage` in `/bot new`) denies
the commands in Claude Code, `alfred` refuses them, and the service skips the usage conditions of that bot's
scheduled items. Choosing a preset turns it back on, since it is the default.

Basic is what a bot gets when nothing is chosen: `/bot new` without options, and a bot you create in
@BotFather yourself. The creation assistant starts on Assistant, the recommended one.

Basic, Assistant and Power are the old Minimal, Standard and Broad buttons, so bots set up with those match
them. Power now states `sandbox on` explicitly, which is the default anyway.

## Where they live

`alfred/presets.py`, as data:

- `PRESET_PERMISSIONS`: preset -> permissions it sets. Its order is the order from least to most.
- `PRESET_INFO`: preset -> icon, label, "can", "cannot" and a warning, in plain words.
- `RECOMMENDED`: the preset the creation assistant starts on.
- `SPECIAL_LEVELS`: levels above the presets that are not permission sets: level -> the `BotRecord` flag that
  holds it. Today only `cross` -> `cross_agent`. Its text is in `PRESET_INFO`, like the presets'.
- `ALIASES`: other words `/bot perm` accepts, including the old names and Spanish ones.
- `FREEFORM`: `folders`, `allow`, `deny`. These belong to the bot, not to a preset: choosing a preset keeps
  them, and they are ignored when matching a bot to a preset.
- `WORDS`, `MODE_RISK`, `LOOSER`: how a change reads ("+ browser") and which direction grants more.

The functions over that data: `detect` (which preset a bot's permissions are, and the closest one),
`apply` (a bot's permissions with a preset chosen), `name_of` ("⭐ Assistant" or
"🎛 Custom (from ⭐ Assistant: + browser)"), `level_name` (the same plus any special level, such as
"🔒 Basic + ⚠️ Cross-agent access"), `gains` (what a change newly allows), `card`, `keys` (the presets) and
`levels` (the presets, then the special levels). `find` only ever names a preset, never a special level.

Nothing about presets is stored on a bot. A bot keeps the permissions it always had, and its preset is worked
out each time it is shown. Bots created before presets need no migration and nothing is rewritten when they
are read.

## In the menus (`alfred/menus.py`)

- **Create bot, step 5**: a button per preset (the current one marked ●) with each preset's "can / cannot /
  warning" in the message, then **🎛 Customize**. Picking a preset goes straight to the summary.
  Customize opens the per-permission switches, starting from the chosen preset. Its **Back** returns to the
  presets and keeps the changes; its **Next** goes to the summary.
- **Summary** and a bot's page: `Permissions: <preset>`, or `Custom (from <base>: <changes>)`. In the
  assistant the base is the preset you started from; for a bot, it is the closest preset.
- **My bots → Permissions**: `Now: <preset or Custom>[ + ⚠️ Cross-agent access]`, the presets as buttons,
  Customize, and below them the cross-agent button (`🔒 Cross-agent access: off (above full access)`, or
  `⚠️ CROSS-AGENT ACCESS ON · withdraw`) with its card and the blacklist line. That button opens the
  security-control warning (`bot_cross`) exactly as before; the Customize screen shows it too. The assistant
  never offers it. A preset that
  grants anything the bot does not have yet opens a confirmation listing what it gains ("It gains: + web,
  + browser, ..."); one that only takes away applies at once. Toggling a single switch under Customize applies
  at once, as before.

A preset is applied in one write by `BotPool.set_permissions` (`alfred/telegram_bot.py`). Like
`set_permission`, it applies from the bot's next message.

## Text shortcuts

- `/bot perm <bot> <preset>`, or `/bot perm <bot> preset <preset>`. It takes a key, a label or an alias
  (`assistant`, `"Full access"`, `standard`, `mínimo`, ...). The reply names the preset and repeats the
  warnings. It does not ask for confirmation, because the command is explicit.
- `/bot perm <bot> <permission> <value>` works as before.
- `/bot show` ends with `Preset: ...`, including cross-agent access when the bot has it.
- `/bot perm <bot> cross on|off` is security-control's command, unchanged; `/bot perm <bot> preset cross` is
  refused, since no preset grants it.

## The SOUL.md under every preset

Each bot's `SOUL.md` (its role and mission as well as its personality: what it does, never what it may do)
is its owner's to change, from the panel. Letting a bot edit it lets it change its own mission. No preset lets a bot edit it: that needs the
`soul` switch under Customize, which opens only the bot's own `SOUL.md` (`Edit` allowed, out of the sandbox's
`denyWrite`), never another bot's. Without it, `Edit` of the file is denied, the sandbox's `denyWrite` covers
it, and with an open shell `Bash(*SOUL.md*)` is denied too. As with everything else, a bot with an
unsandboxed shell (Full access) can get around the text rules.

## The shared memory under every preset

Every agent reads the shared memory (`~/.alfred-shared/memory`), whatever its preset and with or without
cross-agent access. Its index is loaded into each conversation through the agent's copy,
`SHARED_MEMORY.md`, and the folder is an extra working directory and in the sandbox's `allowRead`. It sits
outside Consiglieri's home and the bots' directories, so no blacklist rule covers it. No preset lets a bot write
it. Writing needs the `shared` switch under Customize. A bot with an unsandboxed shell (Full access) can
still write it with shell commands, as it can anything else. `docs/AGENT_CHATS_RESEARCH.md` §6.4, "Memoria
compartida", has the details and the remaining ways around.

## The calendar under every preset

The `calendar` permission ([docs/CALENDAR.md](CALENDAR.md)) gives a bot the Google Calendar connector of the
user's claude.ai account: reading events and creating them. It is off in every preset and template, Full access
included, and is turned on only from Customize (📅), with `/bot perm <bot> calendar on` or `+calendar`. Without it
the connector's tools are denied even with `mcp on` and `bypassPermissions`; with it they are allowed even with
`mcp off`. For a bot admin it is one of the extras that wait for the user's yes.

## Bot admins under every preset

The `admin` permission (docs/BOT_ADMIN.md) lets a bot ask the panel to create bots for its owner from a spec, up to
its own permissions. It is off in every preset, Full access included. The **🧑‍💼 Bot admin** template
(`presets.TEMPLATES`: Full access + `admin`) sits next to the presets on the preset screens, outside their order, and
a bot matching it exactly reads as "🧑‍💼 Bot admin". Turning `admin` on for an existing bot, from the menu or with
`/bot perm`, always shows `bots.ADMIN_WARNING` and asks to confirm; every grant and withdrawal is in the audit log.

## Extending it

- **A new permission** (for example the usage monitor's): add it to `bots.PERMISSIONS` with its default. It
  is then in every preset at that default, a bot that differs reads as Custom, and the change list shows it
  as `name: value` (`+ name` / `− name` if it is in `bots.SWITCHES`). To put it in a preset, add one key to
  that preset in `PRESET_PERMISSIONS`. For a short name in change lists, add it to `WORDS`. If its looser
  value is not `on`, add it to `LOOSER`. An unknown permission counts as "grants more", so a change to it is
  always confirmed. To show it under Customize, add it to `menus.SWITCH_LABELS`.
  The usage monitor's `usage` permission (`docs/USAGE_MONITOR_RESEARCH.md`, section 11) was added this
  way: off in every preset, `"usage": "plan usage"` in `WORDS`, and a row in `menus.SWITCH_LABELS`. Its
  default later became `on` (one value in `bots.PERMISSIONS`), which put it in every preset, with the rule
  that the bot uses it only with the user's express permission (above).
  So was `shared`, writing the shared memory: off in every preset (Full access included), only under
  Customize, `"shared": "shared-memory writes"` in `WORDS`, and a warning that every other agent reads what
  it writes.
- **A new preset**: one entry in `PRESET_PERMISSIONS` at its place in the order, and one in `PRESET_INFO`.
  The buttons, cards, matching, confirmation and text shortcut pick it up.
- **A level above the presets that is not a permission set** (as cross-agent access is): one entry in
  `SPECIAL_LEVELS` naming its `BotRecord` flag, and one in `PRESET_INFO`. Names and cards pick it up; how it
  is granted stays with the code that owns the flag.
  `tests/test_presets.py` checks that every preset has text, names only real permissions, and that each
  grants more than the one before it.
- **Rules that hold at every level**: they belong in `bots.claude_settings`, not in a preset. The blacklist
  that keeps every bot out of other agents' memory and tools is there (`bots.blacklist`). Presets only
  choose permission values, so it applies the same under every preset, Full access included.

## Files touched

- `alfred/presets.py` (new): the presets and the functions above.
- `alfred/menus.py`: the old `PRESETS` and `PRESET_LABELS` removed. New preset screens (`new_perms`,
  `bot_perms`), Customize screens (`new_perms_custom`, `bot_perms_custom`), the confirmation
  (`bot_perms_confirm`), and the preset name in the summary and on a bot's page.
- `alfred/telegram_bot.py`: `BotPool.set_permissions`, the preset shortcut in `/bot perm`, `Preset:` in
  `/bot show`, the preset in the creation message and in `/help`.

Merged with security-control (refactor at `0e56192`): the cross-agent button moved from the old switches
screen to both the presets screen and Customize (`Menus._cross_row`). The `/bot perm` cross check runs before
the preset shortcut. The blacklist tests now iterate over `presets.keys()` instead of the old
`menus.PRESETS`.
- Tests: `tests/test_presets.py` (new), `tests/test_menus.py`, `tests/test_panel.py`.
