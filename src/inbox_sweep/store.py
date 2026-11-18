"""Local SQLite index of message headers, decisions and review overrides."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from inbox_sweep.model import Decision, Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    date INTEGER NOT NULL,
    from_email TEXT NOT NULL,
    from_name TEXT,
    "to" TEXT,
    cc TEXT,
    subject TEXT,
    labels TEXT,
    list_unsubscribe TEXT,
    list_unsubscribe_post TEXT,
    list_id TEXT,
    precedence TEXT,
    auto_submitted TEXT,
    snippet TEXT,
    has_attachment INTEGER,
    message_id_header TEXT,
    folder TEXT,
    uid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_email);

CREATE TABLE IF NOT EXISTS decisions (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    action TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    protected TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS overrides (
    sender TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK (action IN ('keep', 'trash'))
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    sender TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    model TEXT NOT NULL,
    created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MESSAGE_COLUMNS = [
    "id", "thread_id", "date", "from_email", "from_name", "to", "cc", "subject",
    "labels", "list_unsubscribe", "list_unsubscribe_post", "list_id", "precedence",
    "auto_submitted", "snippet", "has_attachment", "message_id_header", "folder", "uid",
]


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- messages -----------------------------------------------------------
    def upsert_messages(self, messages: Iterable[Message]) -> int:
        cols = ", ".join(f'"{c}"' for c in MESSAGE_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in MESSAGE_COLUMNS)
        rows = [m.to_row() for m in messages]
        with self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO messages ({cols}) VALUES ({placeholders})", rows
            )
        return len(rows)

    def messages(self) -> list[Message]:
        cur = self.conn.execute("SELECT * FROM messages ORDER BY date DESC")
        return [Message.from_row(r) for r in cur]

    def message_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def known_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM messages")}

    def clear_messages(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM messages")
            self.conn.execute("DELETE FROM decisions")

    # -- decisions ----------------------------------------------------------
    def save_decisions(self, decisions: Iterable[Decision]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM decisions")
            self.conn.executemany(
                "INSERT INTO decisions VALUES (:message_id, :thread_id, :action, :category, :reason, :protected)",
                [d.__dict__ for d in decisions],
            )

    def decisions(self) -> dict[str, Decision]:
        cur = self.conn.execute("SELECT * FROM decisions")
        return {r["message_id"]: Decision(**dict(r)) for r in cur}

    # -- overrides from the review page -------------------------------------
    def set_overrides(self, overrides: dict[str, str], replace: bool = True) -> None:
        with self.conn:
            if replace:
                self.conn.execute("DELETE FROM overrides")
            self.conn.executemany(
                "INSERT OR REPLACE INTO overrides VALUES (?, ?)",
                [(s.lower(), a) for s, a in overrides.items() if a in ("keep", "trash")],
            )

    def overrides(self) -> dict[str, str]:
        return {r["sender"]: r["action"] for r in self.conn.execute("SELECT * FROM overrides")}

    # -- AI decisions --------------------------------------------------------
    def save_ai(self, sender: str, action: str, category: str, reason: str, model: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO ai_decisions VALUES (?, ?, ?, ?, ?, ?)",
                (sender.lower(), action, category, reason, model, int(time.time())),
            )

    def ai_decisions(self) -> dict[str, dict]:
        cur = self.conn.execute("SELECT * FROM ai_decisions")
        return {r["sender"]: dict(r) for r in cur}

    # -- meta -----------------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value))
            )

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
