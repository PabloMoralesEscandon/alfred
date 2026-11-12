# Security presets for bots

What a bot may do is offered as one of a few named presets instead of a dozen switches. The switches are
still there, behind **Customize**. This document lists the presets, where they live, how the menus and
`/bot perm` use them, and how to extend them.

## The presets

From least to most. Each is a set of permissions over the defaults in `bots.PERMISSIONS`; anything a preset
does not name keeps its default.

| Preset | Can | Cannot | Permissions it sets |
| --- | --- | --- | --- |
| 🔒 **Basic** | Talk with you, set reminders, keep notes and files in its own folder | Go online, touch your other files, run commands, use your browser, message anyone else | none (the defaults) |
| ⭐ **Assistant** (recommended) | Basic, plus search the web, read web pages, edit files in its work folder | Run commands, use your browser or logins, use your connectors, message anyone else | `web on`, `edit on` |
| 🛠 **Power** ⚠️ | Assistant, plus your browser and any command inside Claude Code's sandbox (writes only in its folders) | Run commands outside the sandbox, use connectors, message others, manage other bots | also `browser on`, `shell full`, `sandbox on`, `mode acceptEdits` |
| 🔓 **Full access** ⚠️⚠️ | Anything your user can do on this Mac, without asking | Almost nothing: an unsandboxed shell gets around the remaining rules | everything `on`, `shell full`, `sandbox off`, `mode bypassPermissions` |

Basic is what a bot gets when nothing is chosen: `/bot new` without options, and a bot you create in
@BotFather yourself. The creation assistant starts on Assistant, the recommended one.

Basic, Assistant and Power are the old Minimal, Standard and Broad buttons, so bots set up with those match
them. Power now states `sandbox on` explicitly, which is the default anyway.

## Where they live

`alfred/presets.py`, as data:

- `PRESET_PERMISSIONS`: preset -> permissions it sets. Its order is the order from least to most.
- `PRESET_INFO`: preset -> icon, label, "can", "cannot" and a warning, in plain words.
- `RECOMMENDED`: the preset the creation assistant starts on.
- `ALIASES`: other words `/bot perm` accepts, including the old names and Spanish ones.
- `FREEFORM`: `folders`, `allow`, `deny`. These belong to the bot, not to a preset: choosing a preset keeps
  them, and they are ignored when matching a bot to a preset.
- `WORDS`, `MODE_RISK`, `LOOSER`: how a change reads ("+ browser") and which direction grants more.

The functions over that data: `detect` (which preset a bot's permissions are, and the closest one),
`apply` (a bot's permissions with a preset chosen), `name_of` ("⭐ Assistant" or
"🎛 Custom (from ⭐ Assistant: + browser)"), `gains` (what a change newly allows) and `card`.

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
- **My bots → Permissions**: `Now: <preset or Custom>`, the presets as buttons, and Customize. A preset that
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
- `/bot show` ends with `Preset: ...`.

## Extending it

- **A new permission** (for example the usage monitor's): add it to `bots.PERMISSIONS` with its default. It
  is then in every preset at that default, a bot that differs reads as Custom, and the change list shows it
  as `name: value` (`+ name` / `− name` if it is in `bots.SWITCHES`). To put it in a preset, add one key to
  that preset in `PRESET_PERMISSIONS`. For a short name in change lists, add it to `WORDS`. If its looser
  value is not `on`, add it to `LOOSER`. An unknown permission counts as "grants more", so a change to it is
  always confirmed. To show it under Customize, add it to `menus.SWITCH_LABELS`.
- **A new preset** (for example one above Full access): one entry in `PRESET_PERMISSIONS` at its place in the
  order, and one in `PRESET_INFO`. The buttons, cards, matching, confirmation and text shortcut pick it up.
  `tests/test_presets.py` checks that every preset has text, names only real permissions, and that each
  grants more than the one before it.
- **Rules that hold at every level** (such as a default deny list): they belong in `bots.claude_settings`,
  not in a preset. Presets only choose permission values.

## Files touched

- `alfred/presets.py` (new): the presets and the functions above.
- `alfred/menus.py`: the old `PRESETS` and `PRESET_LABELS` removed. New preset screens (`new_perms`,
  `bot_perms`), Customize screens (`new_perms_custom`, `bot_perms_custom`), the confirmation
  (`bot_perms_confirm`), and the preset name in the summary and on a bot's page.
- `alfred/telegram_bot.py`: `BotPool.set_permissions`, the preset shortcut in `/bot perm`, `Preset:` in
  `/bot show`, the preset in the creation message and in `/help`.
- Tests: `tests/test_presets.py` (new), `tests/test_menus.py`, `tests/test_panel.py`.
