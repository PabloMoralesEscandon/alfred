# Consiglieri refactor: plan

Status: phase 1 is implemented on the `refactor` branch and verified. Phases 2 to 4 are proposals.
The `agent-chats` branch turns the main bot into a control panel that creates one Telegram bot per agent,
each with its own settings, permissions, memory, tools and session; see
[AGENT_CHATS_RESEARCH.md](AGENT_CHATS_RESEARCH.md), section 6.

## Premises

1. Consiglieri is a usable personal bot reachable from anywhere through Telegram.
2. Claude Code is the interface to the model and is assumed by default. Codex stays available as an option.
3. Consiglieri does not ship a catalogue of skills. It generates and maintains its own.

## What was wrong with the first version

| First version | Consequence |
| --- | --- |
| Every message built one big prompt (soul, skills, memory, 8 recent messages) and ran `codex exec` from scratch | No real conversation: context was a lossy 8-message window, trimmed to 800 characters each |
| `SKILLS.md` was one hand-written file, cut off at 4000 characters, pasted into every prompt | Skills could not grow, and the bot could not add any |
| Memory changed only on literal "remember ..." prefixes parsed in Python | The model never decided what was worth keeping |
| The model call was a blocking `subprocess.run` with a 120 s timeout inside the async handler | The whole bot froze during a turn; longer tasks died |
| Reminders needed a second process | Two things to keep alive; reminders could only be fixed text |
| Scheduler usage was hardcoded into the prompt | Every new ability meant editing `bot.py` |

## The idea

Stop building prompts. Give the agent a home directory and let Claude Code do what it already does there.

Claude Code, run headless in a directory, natively loads `CLAUDE.md` (with `@file` imports), discovers
skills in `.claude/skills/`, resumes sessions by id, compacts long conversations, and can write files.
So "an agent that writes its own skills" needs no skill engine. It needs a directory the agent owns, a
manual telling it to save what it learns, and a runtime that gets messages in and out. The runtime
contains no domain logic at all.

```text
 Telegram ──► telegram_bot.py ──► agent.py ──► backends/claude.py ──► claude -p --resume <id>
   ▲            auth, files,       per-chat      (or codex.py)            cwd = ALFRED_HOME
   │            /commands,         lock and                                 │ reads CLAUDE.md, SOUL.md,
   │            progress           session                                  │ memory/,   .claude/skills/*
   │                                  ▲                                     │ writes memory/, new skills  
   │                                  │ scheduled tasks                     ▼
   └──────── scheduler.py ◄── SQLite ◄──────────────── bin/alfred schedule | send | skills
```

## Design decisions

**The home lives outside the repo** (`~/.alfred`, set by `ALFRED_HOME`). Code is the runtime; the home
is the agent's state. This keeps self-modification away from the source tree and makes the old blanket
`*.md` gitignore unnecessary.

**The home is its own git repo, committed after every turn.** Self-written skills are the risky part of
this design. With history, a bad skill or a wrong memory is a `git revert`, and `git log --stat` is an
audit trail of what Consiglieri taught itself.

**Memory is a folder of topic files behind a small index.** Only `memory/MEMORY.md` is loaded into
every conversation; it holds one line per file, and Consiglieri opens the files it needs. This keeps the
always-loaded part small however much is remembered. Consiglieri saves when asked and also on its own
judgment. Because autonomous saving is invisible by nature, the runtime fingerprints the folder before
and after each turn and appends what changed to the reply, independent of what the model says.

**One managed file, the rest owned.** `CLAUDE.md` is the contract between runtime and agent and is
rewritten on every start, so runtime upgrades reach existing homes. `SOUL.md`, the memory folder and every
skill are created once and then belong to the user and the agent.

**Conversation state is the backend's session, not ours.** We store one session id per chat and backend
and pass `--resume`. A lost session falls back to a fresh one automatically. `/new` drops the id.

**Per-turn context goes in the message, not the system prompt.** Claude Code records the system prompt
once per conversation, so the time and chat id travel as a `<context .../>` line on each message.

**Abilities are skills, including the built-in ones.** Scheduling is taught by a seeded `reminders`
skill that documents the `alfred schedule` CLI. Adding an ability never requires touching the runtime
prompt. Only two skills are seeded: `reminders` and `create-skill`, the one that teaches skill writing.

**The agent talks back to the runtime through a CLI on its PATH** (`bin/alfred`). The chat id comes from
the environment, so the model never has to pass ids around, which was a source of errors before.

**Receiving and replying are separate.** The Telegram side hands a message to the agent
(`Agent.submit`) and returns; results come back through a listener whenever a turn ends. With Claude,
each chat has one process that stays open (`claude --print --input-format stream-json`): it sleeps
between messages, takes a message that arrives mid-turn at its next step, and starts a turn by itself
when one of its background commands finishes. Messages and results are therefore not paired: one
result can answer two messages, and some results answer none. This replaced "one process per message",
which could not be corrected mid-task, could not be woken, and capped every job at one turn. The
process is tracked per message through the CLI's `command_lifecycle` events, restarted fresh when its
stored session is gone, and stopped when a turn shows no sign of life for `ALFRED_TIMEOUT` seconds.
`/stop` kills it; the next message resumes the conversation in a new one.

**The last message of a turn is the reply, and the agent can stay quiet.** Its final text is posted,
`alfred send` posts earlier, and a final message of exactly `NO_REPLY` posts nothing. Posting only
through `alfred send` was considered and rejected: a progress note followed by the answer in the final
text is the natural pattern, and it would have lost the answer.

**Anything can wake the agent.** `alfred event "text"` queues a note that reaches the chat's agent
within a couple of seconds as an `[Event]` message. It goes through the scheduler's table, so it works
from any process on the machine and survives a restart of the bot.

**Backends are a small interface.** `run(prompt, session_id, cwd, env, on_progress) -> RunResult` for
one turn, plus `open_live` for backends that can stay open. Codex cannot, so with Codex each message is
one turn and messages wait their turn.
Claude is the default; the backend can be switched per chat with `/backend`. Codex does not discover
`.claude/skills`, so it gets a generated `AGENTS.md` and a skills index in the first message of a session.
Memory and skills are shared between backends; sessions are not.

**Scheduled tasks are agent turns.** A schedule entry is either a fixed message or a prompt. A prompt
is handed to the agent the same way as a user message, in the chat's own session, so "every morning check
X and tell me" works and the follow-up conversation has the context. Entries can repeat.

**One process.** The scheduler is a task inside the bot. Deliveries run concurrently, so a slow agent
task cannot delay a reminder.

**Permissions default to `bypassPermissions`**, matching the old `danger-full-access`, because nobody
is present to approve prompts. It is configurable. The mitigations are the allow-list (now by user id as
well as username), the manual's confirm-first rules, and the option of running under a dedicated user.

**The browser is Orca's, used as a tool; Consiglieri does not live inside Orca.** Three options were
weighed for browser access:

| Option | Verdict |
| --- | --- |
| Orca's built-in browser via the `orca` CLI | Default. Already installed and running here, plain shell commands (so it works headless and with either backend), persistent logins, tabs visible in the app. Verified live. |
| Claude in Chrome (`claude --chrome`) | Supported with `ALFRED_BROWSER=chrome`. Uses the user's real Chrome and its sessions. Claude backend only, and needs a Chrome window with the extension signed in. Verified live. |
| Safari | Rejected. No scripting surface short of desktop control (clicking by screenshot), which is slow and fragile. |

Running the whole agent inside Orca (Telegram messages typed into an Orca terminal, replies scraped
back out) was rejected: it would trade structured JSON results for screen-scraping and make a desktop
app a hard dependency of the bot. Instead the home is registered as an Orca workspace. Orca decides
where a tab opens from the working directory, so commands run from the home land in Consiglieri's own
workspace. The runtime strips the `ORCA_*` context of whichever terminal launched it, so the agent does
not open tabs in, or report status to, that terminal. Orca's scheduled automations overlap with
Consiglieri's scheduler and are not used, because Consiglieri's entries are tied to a chat and its session.

## Phase 1: working core (done)

| Area | Delivered |
| --- | --- |
| Package | `alfred/` replaces the five flat scripts; `pyproject.toml`; `alfred` command |
| Backends | Claude Code (a process that stays open per chat, or one per turn; resume, lost-session retry) and Codex (`exec --json`, resume) |
| Home | Bootstrap, managed manual, seed skills, launcher, git autosave, one-time import of the old layout |
| Agent core | Per-chat session and backend, messages taken mid-turn, self-started turns, new-skill detection |
| Memory | `memory/` folder of topic files with an always-loaded index; saved on request or by Consiglieri's own decision; every change reported in the reply; `/memory` |
| Telegram | Allow-list by id or username, `/help /new /stop /usage /status /skills /schedule /backend`, attachments to inbox, live tool progress, typing indicator, reply splitting, `/stop` kills the process group |
| Scheduler | Messages and agent tasks, one-off and recurring, crash recovery, old database migrated in place |
| Agent tools | `alfred schedule add\|list\|cancel`, `alfred send [--file]`, `alfred event`, `alfred skills` |
| Orca agents | Seeded `orca-agents` skill with a watcher script: launch agents in Orca projects, get woken when they stop, report |
| Terminal | `alfred chat` talks to the same agent without Telegram |
| Service | `alfred service install\|status\|logs\|restart\|uninstall`: launchd agent on macOS, systemd user unit on Linux |
| Browser | Seeded `browser` skill for Orca's built-in browser, `alfred browser-setup`, `alfred screenshot`, browser state in `/status` |
| Tests | 125 tests using stand-in `claude`/`codex`/`orca` binaries and a stand-in Telegram |

Verified live with real Claude Code through `alfred chat`: it scheduled a reminder using the seeded
skill, wrote a fact to memory, recalled both in the resumed session, wrote itself a `disk-report` skill
when asked to learn a procedure, and used that skill in a brand-new conversation.

Browser verified live the same way: Consiglieri opened Hacker News in Orca's browser, clicked through to
another page, read it, saved a screenshot and closed its tab, all inside its own Orca workspace. The
same task was then repeated through Claude in Chrome with `ALFRED_BROWSER=chrome`.

Not verified live:

- **A real Telegram round trip.** No bot token was used. The Telegram layer is covered by tests with
  stand-in objects against python-telegram-bot 22.5.
- **Codex end to end.** The Codex CLI on this machine rejects its configured model
  (`gpt-5.6-sol` is "not supported when using Codex with a ChatGPT account"). The backend is tested
  against the event format that CLI emitted.

## Phase 2: make it pleasant to use daily

- **Desktop control.** Orca also ships `orca computer ...` (accessibility-tree control of any app).
  Seeding a skill for it would give Consiglieri Safari and native apps. Left out for now because it is a
  much larger blast radius than a browser.
- **Browser without the app window.** `orca serve` runs a headless Orca runtime; check whether the
  browser works there, for machines with no desktop session.
- **Formatting.** Replies are plain text today. Convert the model's Markdown to Telegram HTML with a
  plain-text fallback.
- **Voice.** Voice notes already land in the inbox. Transcribe them before the turn (local Whisper or
  an API) so talking to Consiglieri works. A good first candidate for Consiglieri to build as its own skill.
- **Inline approvals.** Instead of all-or-nothing permissions, route Claude Code's permission prompts
  to Telegram as Approve/Deny buttons (`--permission-prompt-tool` with a small MCP server). This is the
  real answer to the security trade-off and would let the default mode be stricter.
- **A gentler `/stop`.** `/stop` kills the process, which also ends its background commands. The CLI
  advertises an interrupt request that would stop only the current turn.
- **Spend cap.** `/usage` shows the Claude plan limits. Add an optional cap that pauses scheduled tasks when a limit is nearly used up.

## Phase 3: self-improvement beyond "write a skill when told"

- **Reflection.** A recurring scheduled task where Consiglieri reviews recent conversations for repeated
  requests and corrections, then proposes skills or memory edits. It reports what it changed.
- **Skill hygiene.** Track how often each skill is used and whether the turn ended in an error; let the
  reflection pass merge duplicates, fix failing skills and retire unused ones.
- **Skills with code.** Encourage `scripts/` next to `SKILL.md` and have Consiglieri run a script once before
  saving the skill. The seeded `create-skill` text already asks for this; add a check.
- **Review gate (optional).** A mode where new or changed skills are sent to the chat as a diff and only
  kept after approval. The git history makes this cheap: commit on approve, `git checkout` on deny.
- **Self-extension of the runtime.** Let Consiglieri add MCP servers and hooks to its own
  `.claude/settings.json`, behind the review gate.

## Phase 4: reach

- **Group chats and multiple users** with per-user memory folders instead of one shared `memory/`.
- **Webhooks in.** An HTTP endpoint so other systems (mail rules, CI, home automation) can hand Consiglieri
  a prompt. `alfred event` is the local half of this already.
- **Remote homes.** Run the agent in a container or on another machine while the Telegram process stays
  small; the backend interface is the seam for this.

## Open questions

1. **Unattended recovery.** The service needs a logged-in session, so a reboot without automatic login leaves Consiglieri down until someone logs in.
2. **How much autonomy by default?** Phase 1 keeps the old behaviour (full access). Inline approvals
   would allow a safer default at the cost of more taps.
3. **One memory or one per chat?** Phase 1 uses a single memory folder for a single-owner bot. The old
   version kept memory per chat.
4. **Seed skills on upgrade.** They are copied once and then owned by Consiglieri, so later improvements to
   the shipped `reminders` text do not reach existing homes. Versioned seeds with a merge prompt would
   fix that.
