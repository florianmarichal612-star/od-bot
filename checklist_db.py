"""
Хранилище чек-листов визитов ОД — SQLite.

ВАЖНО: на Railway файловая система эфемерна между деплоями, если не
подключён Volume. Смонтируй Volume (например на /data) и укажи
CHECKLIST_DB_PATH=/data/checklist.db — иначе история визитов будет
теряться при каждом редеплое.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("CHECKLIST_DB_PATH", "checklist.db")

_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    global _initialized
    if _initialized:
        return
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                chat_id INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                author_name TEXT,
                city TEXT,
                dates TEXT,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.commit()
    _initialized = True


def save_draft(chat_id: int, data: dict):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO drafts (chat_id, data, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
            (chat_id, json.dumps(data, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


def get_draft(chat_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT data FROM drafts WHERE chat_id = ?", (chat_id,)).fetchone()
        return json.loads(row["data"]) if row else None


def save_visit(chat_id: int, author_name: str, city: str, dates: str, data: dict) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO visits (chat_id, author_name, city, dates, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, author_name, city, dates, json.dumps(data, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
        return cur.lastrowid


def list_visits(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, author_name, city, dates, created_at FROM visits ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_visit(visit_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM visits WHERE id = ?", (visit_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result
