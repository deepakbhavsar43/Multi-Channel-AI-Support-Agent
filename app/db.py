"""SQLite storage for the conversation log, sessions and the mock clinic backend.

One file keeps the demo easy to deploy. Every function opens its own short-lived
connection, so the module is safe to call from FastAPI's thread pool.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings

SCHEMA = """
PRAGMA journal_mode=WAL;

-- conversation + observability -------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    user_id         TEXT PRIMARY KEY,          -- e.g. whatsapp:+9198xxxxxxx or web:abc123
    channel         TEXT NOT NULL,
    thread_id       TEXT NOT NULL,             -- LangGraph memory thread
    status          TEXT NOT NULL DEFAULT 'bot',   -- bot | handoff
    handoff_ticket  TEXT,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    channel     TEXT NOT NULL,
    direction   TEXT NOT NULL,                 -- in | out
    sender      TEXT NOT NULL,                 -- user | bot | human_agent | system
    text        TEXT NOT NULL,
    meta        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id, id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    args        TEXT,
    result      TEXT,
    ok          INTEGER NOT NULL,
    error       TEXT,
    latency_ms  INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT,
    user_id     TEXT,
    type        TEXT NOT NULL,                 -- handoff | fallback | error | grounding_reject | guard ...
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, id);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  TEXT PRIMARY KEY,              -- Twilio MessageSid, for idempotency
    created_at  TEXT NOT NULL
);

-- mock clinic backend ------------------------------------------------------
CREATE TABLE IF NOT EXISTS appointments (
    ref           TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    patient_name  TEXT NOT NULL,
    service       TEXT NOT NULL,
    dentist       TEXT NOT NULL,
    start_at      TEXT NOT NULL,               -- ISO, clinic local time
    notes         TEXT,
    status        TEXT NOT NULL DEFAULT 'booked',
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_slot
    ON appointments(dentist, start_at) WHERE status = 'booked';

CREATE TABLE IF NOT EXISTS tickets (
    ref         TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    thread_id   TEXT,
    category    TEXT NOT NULL,
    priority    TEXT NOT NULL,
    summary     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',  -- open | resolved
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_orders (
    order_id       TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    patient_name   TEXT NOT NULL,
    item           TEXT NOT NULL,
    status         TEXT NOT NULL,
    expected_date  TEXT,
    updated_at     TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    return Path(get_settings().db_path)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    found = rows(sql, params)
    return found[0] if found else None


def execute(sql: str, params: tuple = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or 0


# --- logging helpers ---------------------------------------------------------

def log_message(thread_id: str, user_id: str, channel: str, direction: str,
                sender: str, text: str, meta: dict | None = None) -> None:
    execute(
        "INSERT INTO messages(thread_id,user_id,channel,direction,sender,text,meta,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, user_id, channel, direction, sender, text,
         json.dumps(meta, default=str) if meta else None, utcnow()),
    )


def log_tool_call(thread_id: str, name: str, args: dict, result: Any, ok: bool,
                  error: str | None, latency_ms: int) -> None:
    execute(
        "INSERT INTO tool_calls(thread_id,name,args,result,ok,error,latency_ms,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, name, json.dumps(args, default=str), json.dumps(result, default=str),
         int(ok), error, latency_ms, utcnow()),
    )


def log_event(type_: str, thread_id: str | None = None, user_id: str | None = None,
              **detail: Any) -> None:
    execute(
        "INSERT INTO events(thread_id,user_id,type,detail,created_at) VALUES (?,?,?,?,?)",
        (thread_id, user_id, type_, json.dumps(detail, default=str), utcnow()),
    )


def mark_processed(message_id: str) -> bool:
    """Return True the first time a message id is seen (idempotency for webhook retries)."""
    try:
        execute("INSERT INTO processed_messages(message_id,created_at) VALUES (?,?)",
                (message_id, utcnow()))
        return True
    except sqlite3.IntegrityError:
        return False
