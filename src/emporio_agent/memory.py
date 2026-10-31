"""Conversation persistence in SQLite (standard library, zero setup).

The full Gemini message history (including function calls/responses and thought signatures) is stored
per session, so a customer can come back later and the agent still has the context — and so the
history replayed to the API is exactly what the model produced (append-only).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ConversationStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                   session_id TEXT NOT NULL,
                   seq        INTEGER NOT NULL,
                   role       TEXT NOT NULL,
                   content    TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   PRIMARY KEY (session_id, seq))"""
        )
        self._conn.commit()

    def load(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        return [{"role": role, "parts": json.loads(content)} for role, content in rows]

    def append_many(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Append a whole turn atomically (a failed turn never leaves a half-written history)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            (start,) = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()
            self._conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                [(session_id, start + i, m["role"], json.dumps(m["parts"], ensure_ascii=False), now)
                 for i, m in enumerate(messages)],
            )

    def list_sessions(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_id FROM messages GROUP BY session_id ORDER BY MAX(created_at) DESC"
        ).fetchall()
        return [r[0] for r in rows]

    def clear(self, session_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
