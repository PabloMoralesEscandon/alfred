from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import json

import shutil, os



MAX_MEMORY_MESSAGES = 8
MAX_RECENT_ITEM_CHARS = 800
MAX_CURRENT_REQUEST_CHARS = 4000
MAX_PROMPT_FILE_CHARS = 4000
TELEGRAM_MESSAGE_LIMIT = 4000
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
MEMORY_ROOT = Path("memory/chats")
message_memory = defaultdict(lambda: deque(maxlen=MAX_MEMORY_MESSAGES))


def chat_dir_path(chat_id):
    chat_dir = MEMORY_ROOT / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    return chat_dir


def chat_memory_path(chat_id):
    return chat_dir_path(chat_id) / "memory.md"


def recent_memory_path(chat_id):
    return chat_dir_path(chat_id) / "recent.json"


def read_memory(chat_id):
    path = chat_memory_path(chat_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def read_env_lines():
    if not ENV_PATH.exists():
        return []

    try:
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def load_allowed_usernames():
    usernames = set()
    for line in read_env_lines():
        line = line.split("#", 1)[0]
        for token in line.replace(",", " ").split():
            if token.startswith("whitelisted@"):
                username = token.removeprefix("whitelisted@").lstrip("@").strip()
                if username:
                    usernames.add(username.lower())

    return usernames


def is_allowed_user(username):
    if not username:
        return False
    return username.lstrip("@").lower() in load_allowed_usernames()


def get_env_value(name):
    prefix = f"{name}="
    for line in read_env_lines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        return line[len(prefix):].strip().strip('"').strip("'")
    return os.environ.get(name, "").strip()


def get_telegram_token():
    token = get_env_value("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")
    return token


def read_prompt_file(name):
    path = BASE_DIR / name
    if not path.exists():
        return ""

    try:
        return trim_text(path.read_text(encoding="utf-8"), MAX_PROMPT_FILE_CHARS)
    except OSError:
        return ""


def normalize_memory_item(text):
    return " ".join(text.strip().split())


def trim_text(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "\n[trimmed]"


def load_recent(chat_id):
    if message_memory[chat_id]:
        return

    path = recent_memory_path(chat_id)
    if not path.exists():
        return

    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    if not isinstance(items, list):
        return

    dropped_bad_items = False
    for item in items[-MAX_MEMORY_MESSAGES:]:
        if isinstance(item, str):
            if item.startswith("Bot: OpenAI Codex "):
                dropped_bad_items = True
                continue
            message_memory[chat_id].append(item)

    if dropped_bad_items:
        save_recent(chat_id)


def save_recent(chat_id):
    path = recent_memory_path(chat_id)
    items = list(message_memory[chat_id])[-MAX_MEMORY_MESSAGES:]
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remember_recent(chat_id, role, text):
    load_recent(chat_id)
    message_memory[chat_id].append(f"{role}: {trim_text(text, MAX_RECENT_ITEM_CHARS)}")
    save_recent(chat_id)


async def send_bot_message(chat_id, text, *, update=None, record_recent=True):
    response = text[:TELEGRAM_MESSAGE_LIMIT]
    if record_recent:
        remember_recent(chat_id, "Bot", response)

    if update is not None and update.message is not None:
        await update.message.reply_text(response)
        return

    bot = Bot(get_telegram_token())
    await bot.send_message(chat_id=chat_id, text=response)


async def send_reminder(chat_id, message):
    response = f"Reminder: {trim_text(message, TELEGRAM_MESSAGE_LIMIT - 16)}. Good."
    await send_bot_message(chat_id, response, record_recent=True)


def parse_memory_command(msg):
    text = msg.strip()
    lower = text.lower()

    remember_prefixes = (
        "remember that ",
        "please remember that ",
        "please remember ",
        "/remember ",
        "/memorize ",
        "remember ",
    )
    forget_prefixes = (
        "forget that ",
        "please forget that ",
        "please forget ",
        "/forget ",
        "forget ",
    )

    for prefix in remember_prefixes:
        if lower.startswith(prefix):
            return "remember", normalize_memory_item(text[len(prefix):])

    for prefix in forget_prefixes:
        if lower.startswith(prefix):
            return "forget", normalize_memory_item(text[len(prefix):])

    if lower in ("/memory", "show memory", "what do you remember"):
        return "show", ""

    return None, ""


def add_memory(chat_id, item):
    if not item:
        return False

    path = chat_memory_path(chat_id)
    existing = read_memory(chat_id)
    lines = [line for line in existing.splitlines() if line.strip()]
    normalized_existing = {line.removeprefix("- ").strip().lower() for line in lines}

    if item.lower() in normalized_existing:
        return False

    lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def forget_memory(chat_id, item):
    if not item:
        return 0

    path = chat_memory_path(chat_id)
    existing = read_memory(chat_id)
    lines = [line for line in existing.splitlines() if line.strip()]
    kept = []
    removed = 0
    needle = item.lower()

    for line in lines:
        fact = line.removeprefix("- ").strip()
        if needle in fact.lower():
            removed += 1
        else:
            kept.append(line)

    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return removed


def current_utc_text():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_prompt(chat_id, user_id, username, msg):
    load_recent(chat_id)
    history = "\n".join(message_memory[chat_id])
    memory = read_memory(chat_id)
    soul = read_prompt_file("SOUL.md")
    skills = read_prompt_file("SKILLS.md")
    parts = [
        "Runtime context:",
        f"- current_utc: {current_utc_text()}",
        f"- chat_id: {chat_id}",
        f"- user_id: {user_id}",
        f"- username: {username}",
    ]
    if soul:
        parts.extend(["", "Soul instructions:", soul])
    if skills:
        parts.extend(["", "Skill instructions:", skills])
    parts.extend([
        "",
        "Memory rules:",
        "There is no summary job. Persistent memory is edited only by explicit remember/forget commands.",
        "Persistent memory contains only facts the user explicitly asked to remember.",
        "Do not infer new persistent memory from recent chat.",
        "Recent message memory is persisted only as short-term chat context.",
        "Never copy recent message memory into persistent memory unless user explicitly asks to remember it.",
        "",
        "Scheduler tool rules:",
        "Use the local scheduler tool when the user asks to schedule, remind, notify, or send a message in the future.",
        "If the requested time is vague or missing, ask a concise follow-up instead of guessing.",
        "Resolve relative dates using current_utc before calling the tool.",
        "The scheduler requires ISO-8601 due timestamps with timezone and stores them in UTC.",
        "Call the tool from this repository root with these forms:",
        "python3 scheduler_tool.py add --chat-id <chat_id> --user-id <user_id> --username <username> --due <ISO-8601> --message <reminder text> --source-request <original request>",
        "python3 scheduler_tool.py list --chat-id <chat_id>",
        "python3 scheduler_tool.py show --id <reminder-id>",
        "python3 scheduler_tool.py cancel --id <reminder-id> --chat-id <chat_id>",
        "The tool prints JSON only. Treat ok:false as a failed scheduling action and explain the validation error briefly.",
        "After a successful add, confirm that the reminder was scheduled and include the scheduled UTC time.",
    ])
    if memory:
        parts.extend(["", "Persistent memory:", memory])
    if history:
        parts.extend(["", "Recent message memory:", history])
    parts.extend(["", "Current request:", trim_text(msg, MAX_CURRENT_REQUEST_CHARS)])
    return "\n".join(parts)


def codex_response(prompt):
    with tempfile.NamedTemporaryFile("r+", encoding="utf-8", delete=True) as output_file:
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--sandbox",
                "danger-full-access",
                "--skip-git-repo-check",
                "--output-last-message",
                output_file.name,
                prompt,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

        output_file.seek(0)
        response = output_file.read().strip()
        error = result.stderr.decode("utf-8", errors="replace").strip()

        if response:
            return response
        if result.returncode != 0 and error:
            return error
        return "No output"


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update.effective_user.username):
        return

    msg = update.message.text
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username

    memory_action, memory_item = parse_memory_command(msg)
    if memory_action == "remember":
        saved = add_memory(chat_id, memory_item)
        response = "Remembered. Good." if saved else "Already remembered. Good."
        remember_recent(chat_id, "User", msg)
        await send_bot_message(chat_id, response, update=update)
        return
    if memory_action == "forget":
        removed = forget_memory(chat_id, memory_item)
        response = f"Forgot {removed} item. Good." if removed else "No matching memory. Confused!"
        remember_recent(chat_id, "User", msg)
        await send_bot_message(chat_id, response, update=update)
        return
    if memory_action == "show":
        memory = read_memory(chat_id)
        response = memory or "No persistent memory. Good."
        await send_bot_message(chat_id, response, update=update, record_recent=False)
        return

    prompt = build_prompt(chat_id, user_id, username, msg)
    remember_recent(chat_id, "User", msg)

    try:
        await send_bot_message(chat_id, codex_response(prompt), update=update)

    except Exception as e:
        await send_bot_message(chat_id, "Error: " + str(e), update=update)

def main():
    print("WHOAMI:", os.popen("whoami").read())
    print("PWD:", os.getcwd())
    print("PATH:", os.environ.get("PATH"))
    print("CODEX:", shutil.which("codex"))

    app = ApplicationBuilder().token(get_telegram_token()).build()
    app.add_handler(MessageHandler(filters.TEXT, handle))
    app.run_polling()


if __name__ == "__main__":
    main()
