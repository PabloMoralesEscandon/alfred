# Alfred Telegram Assistant

Alfred is a Telegram personal assistant powered by Codex. It supports whitelisted users, per-chat memory, prompt files for personality and skills, scheduled reminders, and local CLI task workflows.

## What The Bot Does

- Replies to whitelisted Telegram users.
- Keeps short-term recent memory per Telegram chat.
- Stores persistent memory only when the user explicitly asks it to remember something.
- Loads `SOUL.md` and `SKILLS.md` into every Codex prompt when those files exist.
- Schedules reminders through a local SQLite database and a separate worker process.
- Can use local command-line tools such as `tdl` when described in `SKILLS.md`.

## Repository Files

- `bot.py`: Telegram bot entrypoint.
- `scheduler_tool.py`: CLI for creating, listing, showing, and cancelling reminders.
- `scheduler_worker.py`: background worker that delivers due reminders.
- `scheduler_store.py`: SQLite storage layer for reminders.
- `manual_one_minute_reminder.py`: manual smoke-test helper.
- `.env.example`: example local configuration.
- `.gitignore`: ignores secrets, Markdown prompt files, memory, logs, databases, and local runtime files.

Markdown prompt files are intentionally ignored by Git except `README.md`. Create them locally when setting up a bot instance.

## Requirements

- Python 3.11 or newer.
- A Telegram bot token from BotFather.
- Codex CLI installed and authenticated on the machine running the bot.
- Optional: `tdl` installed if you want Alfred to manage local tasks.

Install Python dependencies:

```bash
python3 -m venv venv
venv/bin/pip install python-telegram-bot
```

If you already have a Python environment, installing `python-telegram-bot` there is enough.

## Create A Telegram Bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Choose a bot name and username.
4. Copy the bot token BotFather gives you.
5. Start a chat with your new bot and send it a test message after the bot process is running.

Keep the token private. Do not commit it.

## Configure `.env`

Create a local `.env` file in this directory:

```bash
cp .env.example .env
```

Edit `.env`:

```env
whitelisted@your_telegram_username
TELEGRAM_BOT_TOKEN=1234567890:replace_with_your_real_token
```

Add one `whitelisted@...` entry for each Telegram username allowed to use the bot:

```env
whitelisted@first_user
whitelisted@second_user
TELEGRAM_BOT_TOKEN=1234567890:replace_with_your_real_token
```

The bot reloads `.env` on each message for the whitelist. The Telegram token is read when the bot or worker starts, so restart those processes after changing the token.

## Configure `SOUL.md`

Create `SOUL.md` locally to define Alfred's personality and general behavior:

```markdown
Your name is Alfred.

You are the user's personal assistant. Give short, precise, helpful answers.
You may be lightly funny or sarcastic in casual contexts, but stay serious for important, sensitive, or risky topics.
Be honest about uncertainty and ask concise follow-up questions only when needed.
```

`SOUL.md` is optional. If it is missing, the bot runs with only the built-in runtime instructions.

## Configure `SKILLS.md`

Create `SKILLS.md` locally to describe workflows, tools, and conventions Alfred should follow.

Example:

```markdown
# Obsidian Markdown

Use Obsidian wiki links like `[[Note Name]]` when referring to vault notes.
Use headings, short paragraphs, and simple lists.

# ToDoList CLI

Use `tdl` when the user asks to manage tasks.
Common commands:
- `tdl list`
- `tdl add "Task name" -p medium -d "08-06-2026"`
- `tdl start <id-or-name>`
- `tdl done <id-or-name>`
```

`SKILLS.md` is optional. If present, it is inserted into the Codex prompt before memory and scheduler instructions.

## Run The Bot

From this directory:

```bash
venv/bin/python bot.py
```

The bot uses Telegram long polling. That means it contacts Telegram directly for updates. You do not need to expose a public port for the normal setup.

For a detached process:

```bash
setsid venv/bin/python bot.py > bot.log 2>&1 < /dev/null &
```

## Run The Reminder Worker

Scheduled reminders need a second process:

```bash
venv/bin/python scheduler_worker.py
```

The bot creates reminder rows in `scheduler.sqlite3`. The worker polls that database and sends due reminders through Telegram.

For a detached worker:

```bash
setsid venv/bin/python scheduler_worker.py > scheduler_worker.log 2>&1 < /dev/null &
```

## Scheduled Reminder Commands

Alfred is instructed to use `scheduler_tool.py` when the user asks to schedule, remind, notify, or send a message in the future.

Manual add:

```bash
venv/bin/python scheduler_tool.py add \
  --chat-id 123 \
  --user-id 456 \
  --username your_telegram_username \
  --due 2099-01-01T09:00:00Z \
  --message "Call Sam" \
  --source-request "Remind me to call Sam"
```

List reminders:

```bash
venv/bin/python scheduler_tool.py list --chat-id 123
```

Show one reminder:

```bash
venv/bin/python scheduler_tool.py show --id <reminder-id>
```

Cancel one reminder:

```bash
venv/bin/python scheduler_tool.py cancel --id <reminder-id> --chat-id 123
```

Dates must be ISO-8601 timestamps with timezone. UTC with `Z` is recommended:

```text
2026-06-08T09:00:00Z
```

## Reminder Smoke Test

Use real Telegram `chat_id` and `user_id` values:

```bash
venv/bin/python manual_one_minute_reminder.py --chat-id 123 --user-id 456
venv/bin/python scheduler_worker.py --once
```

If no username is passed, the helper uses the first whitelisted username from `.env`.

## Memory

Memory is stored per Telegram `chat_id`:

```text
memory/chats/<chat_id>/memory.md
memory/chats/<chat_id>/recent.json
```

Private chats get separate memory because each private chat has a different `chat_id`. Group chats share memory by group, because Telegram uses one `chat_id` for the group.

Persistent memory changes only when the user explicitly asks Alfred to remember or forget something. Recent memory is short-term context and is capped.

## Tests

Run the focused test suite:

```bash
PYTHONPATH=tests ../venv/bin/python -m unittest tests.test_prompt_files tests.test_scheduler
```

Or, from inside this directory with dependencies installed locally:

```bash
PYTHONPATH=tests python3 -m unittest tests.test_prompt_files tests.test_scheduler
```

## GitHub Safety

Before committing, verify ignored files are not staged:

```bash
git status --short --ignored
```

These should stay ignored:

- `.env`
- `SOUL.md`
- `SKILLS.md`
- `memory/`
- `*.sqlite3`
- `*.log`
- `venv/`

`README.md`, source files, tests, `.gitignore`, and `.env.example` are intended to be safe to commit.

## Troubleshooting

If the bot does not answer:

- Confirm the Telegram bot process is running.
- Confirm `.env` contains `TELEGRAM_BOT_TOKEN`.
- Confirm your Telegram username appears as `whitelisted@your_username`.
- Confirm you are messaging the correct bot.
- Check `bot.log` if running detached.

If reminders do not send:

- Confirm `scheduler_worker.py` is running.
- Confirm the reminder is due.
- Confirm bot and worker use the same `scheduler.sqlite3`.
- List reminders with `scheduler_tool.py list --chat-id <chat-id>`.

If Telegram reports a polling conflict, more than one bot process is running for the same token. Stop duplicates and keep only one polling process.
