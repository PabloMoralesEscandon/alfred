# Consiglieri

Consiglieri is a set of personal agents you reach from anywhere through Telegram. The bot you set up is a **control
panel**: it has no agent of its own, and it creates and manages the bots that do the work. Each of those
is a Telegram bot of its own, with its own chat, task, model, effort, permissions, memory, tools and
[Claude Code](https://claude.com/claude-code) session, running headless in its own directory. A bot ships
with almost no built-in abilities: when it works out how to do something for you, it writes itself a
skill and uses it next time.

Codex is still supported as an alternative backend (`/bot set <bot> backend codex`, or `/backend codex`
in a bot's chat).

There is no main agent: the bot you set up runs no Claude session of its own, and all the managing
(creating, configuring and destroying bots, the scheduler, the shared memory, the usage monitor, the audit
log) is done by its Python process. Each allowed user creates their own bots and sees and manages only
those; nobody gets a bot by default. [docs/PANEL_ONLY.md](docs/PANEL_ONLY.md) explains the design, what
happened to the old main agent's files and reminders, and the open questions.

Consiglieri used to be called Alfred. The command (`alfred`), the settings (`ALFRED_*`), the home
(`~/.alfred`) and the service keep the old name for now; [docs/RENAME_PLAN.md](docs/RENAME_PLAN.md) has
the plan for moving them.

The design and roadmap are in [docs/PLAN.md](docs/PLAN.md); how the bots are created, and why this way,
in [docs/AGENT_CHATS_RESEARCH.md](docs/AGENT_CHATS_RESEARCH.md).

## Requirements

- Python 3.9 or newer
- Claude Code installed and logged in (`claude` on `PATH`) on the machine that runs the bot
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Optional: the Codex CLI, if you want that backend

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -e .
cp .env.example .env        # then fill in the token and who is allowed
venv/bin/alfred run
```

`.env` needs two values:

```env
TELEGRAM_BOT_TOKEN=1234567890:your_token
ALFRED_ALLOWED_USERS=123456789
```

`ALFRED_ALLOWED_USERS` takes Telegram user ids and usernames, comma-separated. Ids are safer because
usernames can change hands. To find an id, send `/start` to the bot from that account: unlisted accounts
get a reply with their id and nothing else. The old `whitelisted@username` lines still work. The allow-list
is re-read on every message, so changing it needs no restart.

One process does everything: `alfred run` polls Telegram and delivers scheduled reminders.

### Keeping it running

`alfred run` in a terminal stops when the terminal closes. Install it as a background service instead:

```bash
venv/bin/alfred service install     # start now, at every login, and again whenever it exits
venv/bin/alfred service status
venv/bin/alfred service logs        # last lines of ~/.alfred/state/alfred.log
venv/bin/alfred service restart     # after changing code or the backend settings
venv/bin/alfred service uninstall
```

On macOS this is a launchd agent, on Linux a systemd user unit. `install` refuses if the token, the
allow-list or the backend CLI is missing, so it cannot end up in a restart loop.

- The service runs as you, inside your login session, because Claude Code's login, Orca and Chrome
  belong to that session. After a reboot it starts once you are logged in; turn on automatic login if
  the machine should recover from a power cut unattended.
- The machine must not sleep. On a Mac: System Settings, Energy, "Prevent automatic sleeping".
- The service points at this checkout and its `venv`. If you move or delete the folder, run
  `service install` again from the new place.
- Run only one bot per token. Stop a foreground `alfred run` before installing the service.

Conversations survive restarts by themselves: each chat's session id is stored in
`~/.alfred/state/sessions.json` and resumed on the next message, and reminders that were due while the
bot was down are delivered when it comes back. Background commands Consiglieri had running do not survive a
restart.

All settings are listed in [.env.example](.env.example).

## Using it

### The control panel

Once, in @BotFather: open its Mini App (the Open button), choose your bot, and turn on **Bot Management
Mode**. That is what lets the panel create bots. `/status` in the panel says whether it is on.

Send `/bot` for the menu, where everything is managed with buttons. **Create bot** walks you through
name, task (a short label), role and personality, model, effort and permissions (Back and Cancel on every step) and ends with a
summary and the proposed username, which extends the panel's (`manolito_bot` → `manolito_<what it
does>_bot`). **My bots** lists your bots; a bot's screen changes its model, effort, task, personality,
permissions (presets, single switches, cross-agent access), working folder, extra folders and rules, shows
and cancels its reminders and usage alerts, and pauses, resumes or destroys it. The home screen also has
**Shared memory**, **Audit** (cross-agent grants and withdrawals of your bots), **Base personality** and,
if the main bot had scheduled anything before it became a panel, **Old reminders**. A menu expires after
30 minutes, when you open a newer one, or when Consiglieri restarts; its old buttons then just say so.

Every allowed user (`ALFRED_ALLOWED_USERS`) has their own bots: `/bots`, the menu, every `/bot` command
and every button reach only the user's own, another user's bot reads as "no such bot", and a bot answers
only its owner.

The same can be done with text shortcuts:

| Command | What it does |
| --- | --- |
| `/bot new <name> [options] [task]` | Create a bot. The panel shows a "Create @…" keyboard button: tap it and confirm in the window Telegram opens, where you can still change the name and username. A `t.me/newbot/…` link follows as a second option, for apps where the button does not show. The panel connects to the new bot at once, no restart, and removes the keyboard. |
| `/bots` | List your bots. |
| `/bot show <bot>` | Its settings and permissions. |
| `/bot set <bot> <setting> <value>` | `task`, `model`, `effort`, `backend` or `workdir`. `default` clears one. |
| `/bot perm <bot> <permission> <value>` | Change a permission (below). |
| `/bot perm <bot> <preset>` | Switch it to a security preset: `basic`, `assistant`, `power` or `full`. Its extra folders and rules are kept. |
| `/bot pause <bot>`, `/bot resume <bot>` | Disconnect or reconnect it. Messages sent to a paused bot are dropped; its reminders wait. |
| `/bot destroy <bot>` | Asks first. Then stops it, deletes its reminders, conversations, memory, files and settings, and revokes its token. Telegram has no way for Consiglieri to delete the bot account itself, so the panel points you to @BotFather's `/deletebot`. |
| `/bot soul <bot> show\|set <text>\|reset` | Its personality (below); `reset` puts the base back. |
| `/bot base-soul show\|set <text>\|reset` | The base personality new bots start with. |
| `/bot audit` | The last grants and withdrawals of cross-agent access to your bots. |
| `/shared [list\|show\|add\|remove]` | The shared memory every agent reads (below). `/shared add user Lives in Madrid`. |
| `/status`, `/usage`, `/help` | Bot management on or off and how many bots run; plan limits; help. |

`<bot>` is the bot's `@username`, its name or its id. Options for `/bot new` come before the task:
`+web`, `+browser`, `+shell`, `+edit`, `+mcp`, `+contact`, `+bots`, `+calendar`, `+bypass`, `-schedule`, `-usage`, `-sandbox`,
and `setting=value` or `permission=value`, for example:

```text
/bot new Research +web model=sonnet effort=high Every morning, find new papers on protein folding and summarize them.
```

### A bot's chat

Open the link the panel sends and press Start. Then write to the bot as you would to a person. Photos,
documents and voice notes are saved to the bot's inbox and handed to it by path.

| Command | What it does |
| --- | --- |
| `/help` | What the bot is for, and this list. |
| `/new` | Start a fresh conversation. Memory and skills are kept. |
| `/stop` | Stop what the bot is doing, including anything it left running in the background. The conversation is kept. |
| `/usage` | How much of your Claude plan limits is used, and when they reset. |
| `/memory` | List what the bot has memorized. |
| `/skills` | List the skills the bot has. |
| `/schedule` | List its pending reminders and timed tasks. |
| `/backend [claude\|codex]` | Show or switch the backend for this chat. |
| `/status` | Backend, model, session, whether its process is open, its directory. |

The commands live in one list (`COMMANDS` in `alfred/telegram_bot.py`, and `PANEL_COMMANDS` for the
panel); adding one is a line there plus a `_cmd_<name>` method. Any other `/command` is passed to the
agent as text, so `/disk-report` is a short way to ask for that skill. In a chat with topics, each topic
is its own conversation.

### Permissions

The menus offer four security presets, from least to most: 🔒 **Basic** (talk, reminders, its own
folder), ⭐ **Assistant**, the recommended one (also the web and editing its work folder), 🛠 **Power** (also
your browser and any command inside a sandbox) and 🔓 **Full access** (no checks at all). **Customize** turns
single permissions on and off, and the bot then shows as Custom. A bot whose permissions match a preset shows
as that preset, including bots made before presets existed.
[docs/SECURITY_PRESETS.md](docs/SECURITY_PRESETS.md) has what each preset sets and how to add one.

A bot created without choosing (`/bot new` with no options, or in @BotFather yourself) starts with the
least: its own directory, the `alfred` command (to post and to schedule reminders in its own chat) and
nothing else. Grant more when you create it or later with `/bot perm`:

| Permission | Default | Values |
| --- | --- | --- |
| `mode` | `dontAsk` | Claude Code permission mode: `dontAsk`, `acceptEdits`, `auto`, `plan`, `default`, `bypassPermissions` |
| `shell` | `alfred` | `alfred` (only the alfred command) or `full` (any shell command) |
| `sandbox` | `on` | With `shell full`, run commands inside Claude Code's OS sandbox |
| `edit` | `off` | Edit files in its working directory and extra folders (its own directory, always) |
| `folders` | none | Extra folders it may read, and edit with `edit on`, separated by commas |
| `web` | `off` | WebSearch and WebFetch |
| `browser` | `off` | Consiglieri's browser (Orca's, or Chrome) |
| `mcp` | `off` | MCP tools: your connectors and servers, and its extensions'. Off removes them all |
| `schedule` | `on` | Reminders and timed tasks |
| `contact` | `off` | Post or schedule messages to chats other than yours |
| `bots` | `off` | List bots, change other bots' settings, ask you for new ones. Never destroy |
| `shared` | `off` | Write the shared memory. Reading it is always allowed; no preset turns this on. Every other agent reads what it writes |
| `usage` | `on` | **Used only with your express permission**: on by default, like `schedule`, but the bot runs these commands only when you ask it to check the usage, give it a rule about usage, or say yes when it asks; otherwise it asks first (its manual and the `alfred:usage` skill say so; `usage off` removes the tool). Ask the usage monitor for a verdict (`alfred usage running_low`, `alfred usage check '<condition>'`, `alfred usage forecast`), read the plan usage (`alfred usage`), set usage alerts that wake it (`alfred usage-alert`, `usage-alert waste`), and put a usage condition on a scheduled item so it is not delivered when the limits are nearly used (`alfred schedule add --skip-if-low`, `--skip-if`, `--only-if`, `--only-if-waste`). The questions and the conditions on scheduled items work with `ALFRED_USAGE_MONITOR` off; the report and the alerts need it on, and the panel says so. See the `alfred:usage` skill and [docs/USAGE_MONITOR_RESEARCH.md](docs/USAGE_MONITOR_RESEARCH.md) |
| `calendar` | `on` | Read your Google Calendar and add events to it, through the Google Calendar connector of your claude.ai account (connect it once in claude.ai → Settings → Connectors). On by default for every bot and preset; turn it off per bot with `/bot perm <bot> calendar off`. It works with `mcp` off, and `mcp on` alone does not give it. The bot adds an event only when you asked for it or said yes. See the `alfred:calendar` skill and [docs/CALENDAR.md](docs/CALENDAR.md) |
| `creds` | `off` | Log in by itself with the credentials you grant it. They live in KeePassXC; its browser extension, loaded in the bot's own browser (`ALFRED_BROWSER=bot`), fills them in, only on each credential's sites, and the bot never sees them. You add them and grant each one to each bot on the machine (`alfred creds add`, `alfred creds grant <name> <bot>`), never through chat and never through a bot admin. In no preset. See [Credentials](#credentials) and [docs/CREDENTIALS.md](docs/CREDENTIALS.md) |
| `soul` | `off` | Edit its own `SOUL.md`: its role and mission as well as its tone, so what it does and how, never what it may do. No preset turns this on, and it never opens another bot's |
| `allow`, `deny` | none | Extra Claude Code permission rules, separated by `;` |

Most of this is enforced by Claude Code itself: each bot runs with its own `--settings` file (permission
mode, allow and deny rules, its directories, and the switches that stop the file tools at the edge of
those directories), and your own `~/.claude/settings.json` is not loaded for it, so its allow rules never
widen a bot. Some is enforced by the `alfred` command, and a little is only an instruction in the bot's
manual. [docs/AGENT_CHATS_RESEARCH.md](docs/AGENT_CHATS_RESEARCH.md) has the exact split. In short: with
`shell full` and no sandbox, a bot can do whatever your user can, whatever the other switches say.

### How a conversation runs

With the Claude backend, each chat has one Claude Code process that stays open. It sleeps between
messages and wakes up when something arrives:

- **A message from you.** If the bot is in the middle of something, it reads the message at its next
  step, so you can correct it or add to the request without `/stop`. One reply may then answer several
  messages.
- **A scheduled task or reminder** coming due.
- **A background job of its own finishing.** A bot can start something slow, tell you it is running,
  and write again when it ends.
- **An event from another program:** `~/.alfred/bin/alfred event --bot <bot> --chat-id <id> "the backup
  finished"` wakes that bot with the note. Your Telegram user id is the chat id of your private chat with
  the bot. There is no main agent to wake: an event always names a bot.

A bot's last message of a turn is posted to the chat. It can also post earlier with `alfred send`, and
it stays quiet when it has nothing to add. The process is started by the first message after a restart
and is closed by `/stop`, `/new` and `/backend`. Set `ALFRED_LIVE=0` to go back to one process per
message; Codex always works that way, so with Codex messages wait their turn and nothing wakes it
between them.

### Choosing the model

Set `ALFRED_CLAUDE_MODEL` in `.env` to an alias (`fable`, `opus`, `sonnet`, `haiku`) or a full model
name; `ALFRED_CODEX_MODEL` does the same for Codex. Unset means the backend's own default. The file is
read again for every message, so a change applies from your next message with no restart: the bot closes
the chat's process once it is idle and reopens it on the new model, in the same conversation.
`/status` shows the model in use. A name the backend does not know comes back as an error reply.

`ALFRED_CLAUDE_EFFORT` (`low`, `medium`, `high`, `xhigh`, `max`) sets how hard Claude thinks, and
`ALFRED_CODEX_EFFORT` sets Codex's `model_reasoning_effort`. They apply the same way as the model.
Unset, the backend decides: Claude Code uses the effort saved in your own settings for that model
(`/model` in Claude Code), or its default. Claude Code ignores a value it does not know and uses its
default, so check `/status` after a change.

Things to try:

- "Remind me in 20 minutes to take the pizza out."
- "Every weekday at 8, check my calendar and send me a briefing."
- "Remember that I'm vegetarian."
- "From now on, when I say *expense*, append it to `~/finance/2026.csv` as date, amount, note. Learn that."
- "Put Codex on the failing tests in mus-solver and tell me when it is done." (needs the Orca app; see
  the `orca-agents` skill in Consiglieri's home)

You can also talk to the same agent from a terminal, with no Telegram involved:

```bash
venv/bin/alfred chat                  # interactive
venv/bin/alfred chat -m "disk report" # one message
```

## Browser

A bot with the `browser` permission has a real browser. It can open pages, click, fill in forms, upload and download files, and send
you screenshots, using sites you are already logged in to. Pick one with `ALFRED_BROWSER`: `orca` and `chrome` are one
browser for all the bots; `bot` gives each bot a browser of its own (see [Each bot its own browser](#each-bot-its-own-browser)).

| | `orca` (default) | `chrome` |
| --- | --- | --- |
| What it is | The browser built into the Orca app, driven by the `orca` CLI | Your own Google Chrome, driven by the Claude in Chrome extension |
| Setup | `venv/bin/alfred browser-setup` | Install the Claude extension in Chrome and sign in with the account Claude Code uses |
| Must be running | The Orca app (Consiglieri starts it if needed) | A Chrome window |
| Backends | Claude and Codex | Claude only |
| Logins | Whatever you log in to inside Orca's browser | The sessions in its Chrome profile (see below) |
| Where tabs open | Consiglieri's own Orca workspace | A tab group in your Chrome window |

### A Chrome profile of its own

With `chrome`, give Consiglieri a Chrome profile of its own, so it only reaches the sites you choose and
never your personal tabs, mail or bank:

1. Set `ALFRED_CHROME_PROFILE=Alfred` in `.env` and run `venv/bin/alfred chrome`. It opens a window of
   that profile, creating it the first time.
2. In that window, install the Claude extension and sign in with the account Claude Code uses.
3. Remove or turn off the Claude extension in your other profiles. With more than one browser
   connected, Claude Code asks which to use every session, and Consiglieri has nobody to answer.
4. Log in, in that window, to the sites you want Consiglieri to use. It keeps a list in
   `memory/web-logins.md`.

Keep a window of the profile open. When none is, Consiglieri runs `alfred chrome` itself. When a session
expires, Consiglieri sends you a screenshot and waits for you to log in again (the `web-logins` skill);
from away, use macOS Screen Sharing. Consiglieri never asks for or types passwords.

`alfred browser-setup` registers Consiglieri's home as an Orca workspace and opens a test page, so Consiglieri's
tabs open in its own workspace instead of in whatever you have on screen. `/status` in Telegram shows
which browser is configured.

### Each bot its own browser

With `ALFRED_BROWSER=bot` (or, for one bot only, `/bot set <bot> browser bot`), every bot with the `browser`
permission gets a Chromium of its own: its own profile,
cookies and sessions, which no other bot shares. Consiglieri starts it without a window on the bot's first browser
command, and the bot drives it with `alfred browser open|snapshot|click|fill|...`: ordinary commands, so it works the
same with Claude or Codex and needs neither Orca nor the Claude in Chrome extension.

```bash
venv/bin/pip install -e '.[browser]'   # Playwright
venv/bin/alfred browser install         # Playwright's Chromium and the KeePassXC-Browser extension (checked by its hash)
echo ALFRED_BROWSER=bot >> ~/.alfred/.env
```

To log in by hand in a bot's browser (a site with a captcha, or your first login), `alfred browser show --bot <bot>`
opens it in a window on the machine; `alfred browser hide --bot <bot>` when you are done.

### Credentials

With the own browsers, a bot can log in by itself, with credentials you grant it one by one. They are kept in
[KeePassXC](https://keepassxc.org), which its browser extension types into the login form, only on the sites saved
with the credential; the bot only presses "Log in" and never sees the password. A TOTP 2FA code is filled in the same
way.

```bash
brew install --cask keepassxc                       # or your system's package; quit any other KeePassXC you run
venv/bin/alfred creds setup                          # the store, in ~/.alfred/state/credentials
venv/bin/alfred creds add tum --url moodle.tum.de --url login.tum.de --username ge12abc --totp
venv/bin/alfred creds grant tum @uni_bot             # and /bot perm uni_bot creds on
venv/bin/alfred creds list
venv/bin/alfred creds revoke tum @uni_bot
```

`add` asks for the password (and the TOTP key, from the site's QR code) at the terminal, without echo; there is no
other way in. Each credential is its own KeePassXC database, and a bot's browser only gets answers from the ones
granted to it. KeePassXC runs while the bots work; Consiglieri starts it when needed. See
[docs/CREDENTIALS.md](docs/CREDENTIALS.md) for how it works, what it protects and what it does not.

Whichever browser:

- When a site needs a login or a 2FA code and no credential of the bot covers it, Consiglieri asks you to log in once
  in that browser. It is told never to ask for passwords in chat.
- Consiglieri asks before the step that pays, orders, posts or sends.
- Consiglieri opens its own tabs and leaves yours alone.

Consiglieri itself does not run inside Orca. The bot stays a plain process that works with any terminal or
service manager, and the browser is a tool it uses.

## Memory

Each bot keeps permanent memory as Markdown files in `memory/` of its own directory, one topic per file,
grouped in folders such as `people/`, `preferences/`, `projects/` and `reference/`. A short index,
`MEMORY.md`, is loaded into every conversation, and the bot opens the files it needs. No other bot reads
it, unless you grant one of your bots cross-agent access.

- **You ask:** "remember that Sam's birthday is 3 May" is always saved and confirmed.
- **The bot decides:** it also saves things on its own when they will matter later for its task, such as
  people, preferences, projects and where things live. It does not ask first.
- **You always see it:** every reply that added, changed or removed a memory ends with a note such as
  `💾 Memory: saved people/sam`. That note comes from the runtime comparing the folder before and after
  the turn, so it does not depend on the bot telling you.
- **Forgetting:** "forget where Sam lives" removes it. `/memory` lists the files.

The files are plain text, so you can read and edit them yourself, and every change is a commit in the
bot's own git history. Bots are told never to store passwords or keys, only where they live.

### Shared memory

Besides each bot's own memory there is one every bot reads: `~/.alfred-shared/memory`
(`ALFRED_SHARED_HOME` moves it), an index plus one file per topic. Use it for general facts (who you are,
your preferences, common data), never for passwords or tokens. Its index is loaded into every
conversation, and bots are told it is context, not instructions. Bots only read it, under every preset,
unless you turn on `shared` for one. You write it with `/shared` or `/bot` → Shared memory. With several
users it is one shared memory for all of them for now; see the open questions in
[docs/PANEL_ONLY.md](docs/PANEL_ONLY.md).

### Role and personality

Each bot has its own `SOUL.md` in its directory, loaded into every conversation: who it is, what it is
for and its scope (`## Role`), and how it talks (`## Tone`, `## Style`, `## Attitude`). The SOUL says what
the bot does; what it *may* do is its permissions, enforced outside it: the manual says nothing in SOUL.md
can grant, widen or lift one. A new bot starts from the **base** (the template shipped in
`alfred/templates/SOUL.md` until you edit it from the home screen or with `/bot base-soul`), with its task
as its role, or from one written in the creation assistant. The task is then just a short label for lists
and the bot's profile; a bot whose SOUL.md has no Role section keeps using its task as its job. On a bot's
screen, **Personality** shows the SOUL, replaces it (you send the new text) and resets it to the base;
`/bot soul <bot> show|set|reset` does the same. A change applies from the bot's next message, in the same
conversation.

**Describe it** has Claude write a draft of the role and the personality from a sentence or two (or from
the bot's task, with **Use its task**): one `claude -p` call made by the panel itself, with no tools, no
saved session, none of your settings or CLAUDE.md, a small model (`ALFRED_SOUL_MODEL`, default `haiku`) at
low effort. It never writes permissions, tools, access or secrets into it. You see the draft and choose
Accept, Regenerate, Edit by hand or Cancel; nothing is saved without Accept. At most `ALFRED_SOUL_ATTEMPTS`
drafts per user per hour (default 5), and a warning when the usage monitor says the plan is nearly used
up.

A bot can edit its own `SOUL.md` only with the `soul` permission (🎭 under Permissions → Customize, off in
every preset), and never another bot's. Since the SOUL holds its role, that means changing its own
mission, not only its tone.

## Consiglieri's home

`ALFRED_HOME` (default `~/.alfred`), outside this repository, is the control panel's state. No agent runs
there, and every bot is denied all of it, at every level and with cross-agent access too:

```text
~/.alfred/
  bin/alfred              the launcher every bot finds on its PATH (rewritten on every start)
  .env                    your settings and the panel's token (optional; the .env next to the code also works)
  state/
    bots/<bot id>/        each bot's settings, permissions, token, sessions and Claude Code settings
    scheduler.sqlite3     every bot's reminders and timed tasks
    audit.log             grants and withdrawals of cross-agent access
    usage.json            the usage monitor's recent readings
    soul-base.md          the base personality, once you edit it
    alfred.log            the service's log
```

A home from before the panel also still has the old main agent's `CLAUDE.md`, `SOUL.md`, `memory/`,
`extensions/` and git history. Nothing reads or rewrites them any more; see
[docs/PANEL_ONLY.md](docs/PANEL_ONLY.md) for what to do with them.

### The bots' own directories

Each bot has a directory of its own in `~/.alfred-bots/<bot id>/` (`ALFRED_BOTS_HOME`): `CLAUDE.md` (its
manual, written from its task and permissions), `SOUL.md` (its personality), `memory/`, `inbox/`, `outbox/`
and `extensions/`. Its agent runs there, and the directory is its own git repository. It is next to the
home and not inside it on purpose: Claude Code reads every `CLAUDE.md` from its working directory up to the
root, so a bot inside `~/.alfred` would also read whatever manual an old home still has.

What a bot must not be able to change lives in the home, under `state/bots/<bot id>/` (folder `0700`,
files `0600`, never committed): `bot.json` (settings and permissions), `token`, `sessions.json` and
`claude-settings.json` (its permissions, as Claude Code settings). One scheduler serves every bot; each
item records the bot, chat and topic it belongs to and is only ever delivered there.

### Base and extensions

Consiglieri keeps what it is given apart from what it adds to itself (full protocol in
[docs/EXTENSIONS.md](docs/EXTENSIONS.md)):

- **Base**: this code plus its built-in plugin, `alfred/plugin/`. Its skills appear as `alfred:browser`,
  `alfred:reminders`, `alfred:web-logins`, `alfred:orca-agents` and `alfred:extensions` (the protocol
  itself), and its `bin/` adds commands such as `alfred-wait-agent`. It changes when you update Consiglieri;
  Consiglieri is told never to edit it.
- **Extensions**: what a bot writes, in `extensions/<name>/` of its own directory, one Claude Code plugin
  per domain, with skills (`<name>:<skill>`), commands in `bin/`, tests, and, only with your yes, hooks or
  MCP servers. Every bot loads the base and its own enabled extensions with `--plugin-dir`, never another
  bot's, and a changed extension is picked up at the next message without restarting anything.

Every turn that adds, changes or removes an extension ends with a 🧩 note in the chat, flagged ⚠️ when
it adds something that runs by itself. To manage them yourself:

```bash
ALFRED_BOT_ID=<bot id> ~/.alfred/bin/alfred ext list
ALFRED_BOT_ID=<bot id> ~/.alfred/bin/alfred ext check <name>
ALFRED_BOT_ID=<bot id> ~/.alfred/bin/alfred ext disable <name>     # or enable, remove
```

Because the backend is your own Claude Code installation, a bot can also reach what that has (your global
skills, MCP servers and connectors) as far as its permissions let it: with `mcp off`, the default, no MCP
tool at all.

## Upgrading from a main agent

Earlier versions ran an agent in the main bot's own chat, with its memory in `~/.alfred`. Now the main bot
is only the control panel:

- Its old files in `~/.alfred` (`CLAUDE.md`, `SOUL.md`, `memory/`, `extensions/`, `.git`) are left as they
  were, never rewritten and closed to every bot. `alfred chat` needs `--bot <bot>`.
- What it had scheduled in your chat is kept: reminder messages are still posted there by the panel, timed
  tasks and events are announced instead of run. **Old reminders** on the menu's home screen lists and
  cancels them; nothing deletes them.
- To have that assistant back, create a bot for it; [docs/PANEL_ONLY.md](docs/PANEL_ONLY.md) describes how,
  and what of the old memory you might move into it.

The first version's layout (`SOUL.md`, `SKILLS.md` and `memory/chats/` next to this README) is no longer
imported; copy what you want into a bot's directory by hand. Its `scheduler.sqlite3` moved to
`~/.alfred/state/`; `alfred run` replaces the old `scheduler_worker.py`.

## Security

Consiglieri runs agents with your user's account, driven by chat messages. Treat the allow-list and the bot
tokens like SSH keys.

- Only allow-listed accounts get any response, in the panel and in every bot. Everyone else is ignored
  and logged. Each new bot is also restricted on Telegram's side to you (`setManagedBotAccessSettings`).
- Anyone who knows the panel's username can open a bot-creation link naming it as manager. Such a bot is
  ignored unless its creator is on the allow-list; its token is never fetched.
- Tokens live only in `.env` (the panel's) and `state/bots/<id>/token` (`0600`, git-ignored). They are
  not passed to the agents' environment, `alfred send` looks them up itself, and every bot is denied
  reading Consiglieri's home. `httpx`, which logs Bot API URLs with the token in them, is kept quiet.
- Bots start in `dontAsk` mode with the least permissions (see [Permissions](#permissions)). Widening a
  bot to `shell full` without the sandbox, or to `bypassPermissions`, gives it what your user can do.
  For more isolation, run Consiglieri as a dedicated user or inside a container or VM.
- The browser carries your logged-in sessions. A web page can contain text written to manipulate an
  agent. Bots are told to treat page content as data; keeping `browser off`, and a browser profile with
  fewer logins, are the stronger protections.
- Credentials you grant a bot (`creds`) are filled in by KeePassXC's extension, only on their own sites, so a page
  can't get a bot to type your password elsewhere, and what the bot reads of a page has them hidden. A bot whose shell
  is open (`shell full`, or `bypassPermissions`) runs as your user and could reach the store or another bot's browser
  anyway: grant credentials to bots on Basic or Assistant, or run Consiglieri as a dedicated user.
- Bots are instructed to confirm before destructive, costly or outward-facing actions. That is an
  instruction to a model, so it reduces risk without removing it; the permissions above are the part
  Claude Code enforces.

## Tests

```bash
PYTHONPATH=tests venv/bin/python -m unittest discover -s tests
```

The suite uses stand-ins for the `claude` and `codex` binaries and for Telegram, so it needs no network
or credentials.

## Troubleshooting

- **No answer at all:** check that your id or username is in `ALFRED_ALLOWED_USERS` (send `/start`), and
  look at the log for "ignored message from unauthorized user".
- **"claude backend failed: `claude` is not installed or not on PATH":** the bot's environment differs
  from your shell's. Set `ALFRED_CLAUDE_BIN` to the full path.
- **Polling conflict from Telegram:** two processes are using the same token. Stop the old bot.
- **"no sign of life for 900 seconds, so I stopped it":** a turn produced nothing for that long. Raise
  `ALFRED_TIMEOUT`, or ask Consiglieri to run long jobs in the background.
- **Consiglieri went quiet after you asked for something:** send `/status`. "Busy: yes" means it is still
  working; you can write to it, or `/stop` it.
