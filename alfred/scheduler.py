"""Scheduled deliveries: fixed messages and agent tasks, one-off or recurring, in SQLite."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Union

log = logging.getLogger("alfred.scheduler")

STATUS_SCHEDULED = "scheduled"
STATUS_DELIVERING = "delivering"
STATUS_SENT = "sent"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

KIND_MESSAGE = "message"
KIND_TASK = "task"
KIND_EVENT = "event"  # something happened that the agent should know about now

MIN_REPEAT_SECONDS = 60
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DURATION_PATTERN = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)

Item = Dict[str, Any]


class Postpone(Exception):
    """Raised by a delivery that cannot happen yet (its bot is paused): try again in `seconds`."""

    def __init__(self, seconds: int = 300, reason: str = "") -> None:
        super().__init__(reason or "postponed")
        self.seconds = seconds


def parse_duration(text: str) -> int:
    """`90s`, `45m`, `2h`, `1d`, `1w` -> seconds."""
    match = DURATION_PATTERN.match(text)
    if not match:
        raise ValueError("duration must be a number followed by s, m, h, d or w (for example 45m)")
    seconds = int(match.group(1)) * DURATION_UNITS[match.group(2).lower()]
    if seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return seconds


def parse_time(value: Union[str, datetime]) -> datetime:
    """ISO-8601 -> aware UTC datetime. A time without an offset is taken as machine-local."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def to_utc_text(value: Union[str, datetime]) -> str:
    return parse_time(value).isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class SchedulerStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        if not self._ready:
            self._migrate(connection)
            self._ready = True
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                due_at_utc TEXT NOT NULL,
                message TEXT NOT NULL,
                source_request TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                delivered_at_utc TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        # Columns added after the first public version; older databases get them here.
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(scheduled_messages)")}
        if "kind" not in existing:
            connection.execute("ALTER TABLE scheduled_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'message'")
        if "repeat_seconds" not in existing:
            connection.execute("ALTER TABLE scheduled_messages ADD COLUMN repeat_seconds INTEGER")
        # Which bot the item belongs to and the topic it goes to, if any. 0 is the control panel: only items
        # from before the panel have it (see ControlPanel._deliver); nothing new is scheduled there.
        if "bot_id" not in existing:
            connection.execute("ALTER TABLE scheduled_messages ADD COLUMN bot_id INTEGER NOT NULL DEFAULT 0")
        if "thread_id" not in existing:
            connection.execute("ALTER TABLE scheduled_messages ADD COLUMN thread_id INTEGER")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_messages_due ON scheduled_messages(status, due_at_utc)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_messages_chat ON scheduled_messages(chat_id, created_at_utc)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_messages_bot ON scheduled_messages(bot_id, chat_id)")
        connection.commit()

    # -- writes from the CLI ------------------------------------------------

    def add(
        self,
        *,
        chat_id: int,
        due: Union[str, datetime],
        text: str,
        kind: str = KIND_MESSAGE,
        repeat_seconds: Optional[int] = None,
        user_id: int = 0,
        username: str = "",
        bot_id: int = 0,
        thread_id: Optional[int] = None,
    ) -> Item:
        if kind not in (KIND_MESSAGE, KIND_TASK, KIND_EVENT):
            raise ValueError("kind must be 'message', 'task' or 'event'")
        text = text.strip()
        if not text:
            raise ValueError("text must not be empty")
        if repeat_seconds is not None and repeat_seconds < MIN_REPEAT_SECONDS:
            raise ValueError("--every must be at least %d seconds" % MIN_REPEAT_SECONDS)
        item_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduled_messages (
                    id, chat_id, user_id, username, due_at_utc, message, source_request,
                    status, created_at_utc, kind, repeat_seconds, bot_id, thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id, chat_id, user_id, username, to_utc_text(due), text,
                    STATUS_SCHEDULED, to_utc_text(utc_now()), kind, repeat_seconds, bot_id, thread_id or None,
                ),
            )
        created = self.get(item_id)
        assert created is not None
        return created

    def get(self, item_id: str) -> Optional[Item]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM scheduled_messages WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def list(self, chat_id: int, *, include_done: bool = False, bot_id: int = 0) -> List[Item]:
        """A chat's items in one bot. The same chat id in another bot is another conversation."""
        query = "SELECT * FROM scheduled_messages WHERE chat_id = ? AND bot_id = ?"
        parameters: List[Any] = [int(chat_id), bot_id]
        if not include_done:
            query += " AND status IN (?, ?)"
            parameters += [STATUS_SCHEDULED, STATUS_DELIVERING]
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY due_at_utc, created_at_utc", parameters).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, id_or_prefix: str, chat_id: int, bot_id: int = 0) -> Optional[Item]:
        """Cancel a pending item of this chat (in this bot) by id or unambiguous id prefix."""
        prefix = id_or_prefix.strip().lower()
        if not prefix:
            return None
        matches = [item for item in self.list(chat_id, bot_id=bot_id) if item["id"].startswith(prefix)]
        if len(matches) != 1:
            return None
        with self._connect() as connection:
            connection.execute(
                "UPDATE scheduled_messages SET status = ? WHERE id = ?", (STATUS_CANCELLED, matches[0]["id"])
            )
        return self.get(matches[0]["id"])

    def count_for_bot(self, bot_id: int) -> int:
        """Pending items of a bot, in any chat."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM scheduled_messages WHERE bot_id = ? AND status IN (?, ?)",
                (bot_id, STATUS_SCHEDULED, STATUS_DELIVERING),
            ).fetchone()
        return int(row["n"])

    def delete_for_bot(self, bot_id: int) -> int:
        """Remove every item of a bot, done or pending, when the bot is destroyed. Returns how many."""
        if not bot_id:
            raise ValueError("the panel's old items are never deleted in bulk")
        with self._connect() as connection:
            return connection.execute("DELETE FROM scheduled_messages WHERE bot_id = ?", (bot_id,)).rowcount

    # -- worker side --------------------------------------------------------

    def recover_stale(self) -> int:
        """Items left mid-delivery by a crash go back in the queue."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE scheduled_messages SET status = ? WHERE status = ?", (STATUS_SCHEDULED, STATUS_DELIVERING)
            )
            return cursor.rowcount

    def claim_due(self, now: Union[str, datetime, None] = None) -> Optional[Item]:
        now_text = to_utc_text(now or utc_now())
        connection = self._connect()
        try:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM scheduled_messages WHERE status = ? AND due_at_utc <= ?
                ORDER BY due_at_utc, created_at_utc LIMIT 1
                """,
                (STATUS_SCHEDULED, now_text),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE scheduled_messages SET status = ?, attempts = attempts + 1 WHERE id = ?",
                    (STATUS_DELIVERING, row["id"]),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(row["id"]) if row is not None else None

    def postpone(self, item_id: str, seconds: int, *, now: Optional[datetime] = None) -> Optional[Item]:
        """Put an item that could not be delivered yet back in the queue, `seconds` from now."""
        due = to_utc_text((now or utc_now()) + timedelta(seconds=max(1, seconds)))
        with self._connect() as connection:
            connection.execute(
                "UPDATE scheduled_messages SET status = ?, due_at_utc = ? WHERE id = ? AND status = ?",
                (STATUS_SCHEDULED, due, item_id, STATUS_DELIVERING),
            )
        return self.get(item_id)

    def finish(self, item_id: str, *, error: Optional[str] = None, now: Optional[datetime] = None) -> Optional[Item]:
        """Record a delivery attempt. Recurring items are put back with their next due time."""
        item = self.get(item_id)
        if item is None or item["status"] != STATUS_DELIVERING:
            return item  # cancelled while it was being delivered: leave it cancelled
        now = now or utc_now()
        error_text = error[:1000] if error else None
        delivered = None if error else to_utc_text(now)
        if item["repeat_seconds"]:
            step = timedelta(seconds=item["repeat_seconds"])
            due = parse_time(item["due_at_utc"]) + step
            if due <= now:  # skip occurrences missed while the bot was down
                due += step * ((now - due) // step + 1)
            status, due_text = STATUS_SCHEDULED, to_utc_text(due)
        else:
            status, due_text = (STATUS_FAILED if error else STATUS_SENT), item["due_at_utc"]
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scheduled_messages
                SET status = ?, due_at_utc = ?, last_error = ?, delivered_at_utc = COALESCE(?, delivered_at_utc)
                WHERE id = ?
                """,
                (status, due_text, error_text, delivered, item_id),
            )
        return self.get(item_id)


async def run_scheduler(
    store: SchedulerStore,
    deliver: Callable[[Item], Awaitable[None]],
    *,
    poll_seconds: float = 5.0,
    once: bool = False,
) -> None:
    """Deliver due items forever. Each delivery runs as its own task, so a slow agent task never delays a reminder."""
    recovered = store.recover_stale()
    if recovered:
        log.info("requeued %d interrupted item(s)", recovered)
    pending: Set["asyncio.Future[None]"] = set()

    async def deliver_one(item: Item) -> None:
        try:
            await deliver(item)
        except Postpone as wait:
            store.postpone(item["id"], wait.seconds)
        except Exception as exc:  # delivery failures must not stop the loop
            log.exception("delivery of %s failed", item["id"])
            store.finish(item["id"], error=str(exc) or exc.__class__.__name__)
        else:
            store.finish(item["id"])

    try:
        while True:
            while True:
                item = store.claim_due()
                if item is None:
                    break
                task = asyncio.ensure_future(deliver_one(item))
                pending.add(task)
                task.add_done_callback(pending.discard)
            if once:
                if pending:
                    await asyncio.gather(*pending)
                return
            await asyncio.sleep(poll_seconds)
    finally:
        for task in pending:
            task.cancel()
