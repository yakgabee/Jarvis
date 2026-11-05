"""Tiny SQLite store for Jarvis's long-term memory, reminders and text history."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    due_at TEXT NOT NULL,          -- ISO-8601, UTC
    sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sms_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Reminder:
    id: int
    message: str
    due_at: datetime  # aware, UTC


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ── facts ────────────────────────────────────────────
    def add_fact(self, text: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts (text, created_at) VALUES (?, ?)", (text, _now())
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_facts(self) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._conn.execute("SELECT id, text FROM facts ORDER BY id").fetchall()
        return [(row["id"], row["text"]) for row in rows]

    def delete_fact(self, fact_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ── reminders ────────────────────────────────────────
    def add_reminder(self, message: str, due_at: datetime) -> int:
        due_utc = due_at.astimezone(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (message, due_at, created_at) VALUES (?, ?, ?)",
                (message, due_utc, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_reminders(self) -> list[Reminder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, message, due_at FROM reminders WHERE sent = 0 ORDER BY due_at"
            ).fetchall()
        return [
            Reminder(row["id"], row["message"], datetime.fromisoformat(row["due_at"]))
            for row in rows
        ]

    def due_reminders(self, now: datetime) -> list[Reminder]:
        return [r for r in self.pending_reminders() if r.due_at <= now]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
            self._conn.commit()

    def cancel_reminder(self, reminder_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM reminders WHERE id = ? AND sent = 0", (reminder_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── text-message history ─────────────────────────────
    def add_sms(self, role: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sms_history (role, text, created_at) VALUES (?, ?, ?)",
                (role, text, _now()),
            )
            self._conn.commit()

    def recent_sms(self, limit: int = 20) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, text FROM sms_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(row["role"], row["text"]) for row in reversed(rows)]
