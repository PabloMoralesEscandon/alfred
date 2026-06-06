from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "scheduler.sqlite3"

STATUS_SCHEDULED = "scheduled"
STATUS_DELIVERING = "delivering"
STATUS_SENT = "sent"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_utc_text(value: str | datetime) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)

    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")

    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scheduled_messages_due
            ON scheduled_messages(status, due_at_utc)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scheduled_messages_chat
            ON scheduled_messages(chat_id, created_at_utc)
            """
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def create_message(
    *,
    chat_id: int,
    user_id: int,
    username: str,
    due_at_utc: str | datetime,
    message: str,
    source_request: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    message_id: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    item_id = message_id or str(uuid.uuid4())
    now = utc_now_iso()
    due = normalize_utc_text(due_at_utc)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO scheduled_messages (
                id, chat_id, user_id, username, due_at_utc, message,
                source_request, status, created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                chat_id,
                user_id,
                username,
                due,
                message,
                source_request,
                STATUS_SCHEDULED,
                now,
            ),
        )

    created = get_message(item_id, db_path=db_path)
    if created is None:
        raise RuntimeError("scheduled message was not created")
    return created


def list_messages(
    chat_id: int,
    *,
    status: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        if status:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_messages
                WHERE chat_id = ? AND status = ?
                ORDER BY due_at_utc, created_at_utc
                """,
                (chat_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_messages
                WHERE chat_id = ?
                ORDER BY due_at_utc, created_at_utc
                """,
                (chat_id,),
            ).fetchall()

    return [dict(row) for row in rows]


def get_message(
    message_id: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM scheduled_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return row_to_dict(row)


def cancel_message(
    message_id: str,
    chat_id: int,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM scheduled_messages
            WHERE id = ? AND chat_id = ? AND status = ?
            """,
            (message_id, chat_id, STATUS_SCHEDULED),
        ).fetchone()
        if row is None:
            return None

        conn.execute(
            """
            UPDATE scheduled_messages
            SET status = ?
            WHERE id = ? AND chat_id = ? AND status = ?
            """,
            (STATUS_CANCELLED, message_id, chat_id, STATUS_SCHEDULED),
        )

    return get_message(message_id, db_path=db_path)


def claim_due_message(
    now_utc: str | datetime,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    init_db(db_path)
    now = normalize_utc_text(now_utc)

    conn = connect(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM scheduled_messages
            WHERE status = ? AND due_at_utc <= ?
            ORDER BY due_at_utc, created_at_utc
            LIMIT 1
            """,
            (STATUS_SCHEDULED, now),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        conn.execute(
            """
            UPDATE scheduled_messages
            SET status = ?, attempts = attempts + 1, last_error = NULL
            WHERE id = ? AND status = ?
            """,
            (STATUS_DELIVERING, row["id"], STATUS_SCHEDULED),
        )
        claimed = conn.execute(
            "SELECT * FROM scheduled_messages WHERE id = ?",
            (row["id"],),
        ).fetchone()
        conn.execute("COMMIT")
        return row_to_dict(claimed)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def mark_sent(
    message_id: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    init_db(db_path)
    delivered_at = utc_now_iso()
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE scheduled_messages
            SET status = ?, delivered_at_utc = ?, last_error = NULL
            WHERE id = ?
            """,
            (STATUS_SENT, delivered_at, message_id),
        )
    return get_message(message_id, db_path=db_path)


def mark_failed(
    message_id: str,
    error: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE scheduled_messages
            SET status = ?, last_error = ?
            WHERE id = ?
            """,
            (STATUS_FAILED, error[:1000], message_id),
        )
    return get_message(message_id, db_path=db_path)
