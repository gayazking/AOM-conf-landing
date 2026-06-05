"""Async SQLite data layer (aiosqlite).

Schema:
  users            — one row per Telegram user / lead.
  scheduled_jobs   — idempotent drip schedule, re-armed on boot.
  lead_queue       — failed lead-API posts awaiting retry.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import aiosqlite

import config

_db: Optional[aiosqlite.Connection] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id        INTEGER PRIMARY KEY,
    username     TEXT,
    name         TEXT,
    phone        TEXT,
    city         TEXT,
    subscribed   INTEGER NOT NULL DEFAULT 0,
    registered   INTEGER NOT NULL DEFAULT 0,
    paid         INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    funnel_stage TEXT NOT NULL DEFAULT 'new',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id   TEXT PRIMARY KEY,           -- e.g. '12345:funnel_1h'
    tg_id    INTEGER NOT NULL,
    kind     TEXT NOT NULL,              -- funnel_1h / funnel_1d / ...
    run_at   INTEGER NOT NULL,           -- epoch seconds
    sent     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lead_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL,
    payload     TEXT NOT NULL,           -- JSON
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    next_try_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_tg ON scheduled_jobs(tg_id);
CREATE INDEX IF NOT EXISTS idx_jobs_sent ON scheduled_jobs(sent);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL,
    payload    TEXT NOT NULL,            -- JSON Responses-API item
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_tg ON messages(tg_id, id);

CREATE TABLE IF NOT EXISTS ai_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL,
    text       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def _now() -> int:
    return int(time.time())


async def init() -> None:
    """Open the connection and create the schema. Call once at startup."""
    global _db
    import os

    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    # Concurrency hardening: WAL lets the retry worker / scheduler / handlers
    # interleave without 'database is locked', busy_timeout retries instead of
    # raising SQLITE_BUSY, synchronous=NORMAL is durable enough under WAL.
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=5000")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.executescript(SCHEMA)
    # Migrate existing DBs: add AI columns to users if absent (no IF NOT EXISTS
    # for ADD COLUMN in SQLite, so check pragma first).
    async with _db.execute("PRAGMA table_info(users)") as cur:
        _cols = {r["name"] for r in await cur.fetchall()}
    for _c, _ddl in (("ai_paused", "INTEGER NOT NULL DEFAULT 0"),
                     ("ai_paused_until", "INTEGER NOT NULL DEFAULT 0")):
        if _c not in _cols:
            await _db.execute(f"ALTER TABLE users ADD COLUMN {_c} {_ddl}")
    await _db.commit()


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("db.init() must be called before use")
    return _db


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
async def upsert_user_basic(tg_id: int, username: str | None) -> None:
    """Ensure a user row exists; refresh username. Used on /start."""
    now = _now()
    db = _conn()
    await db.execute(
        """
        INSERT INTO users (tg_id, username, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            username = excluded.username,
            updated_at = excluded.updated_at
        """,
        (tg_id, username, now, now),
    )
    await db.commit()


async def get_user(tg_id: int) -> Optional[dict[str, Any]]:
    db = _conn()
    async with db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_subscribed(tg_id: int, subscribed: bool) -> None:
    db = _conn()
    await db.execute(
        "UPDATE users SET subscribed = ?, updated_at = ? WHERE tg_id = ?",
        (1 if subscribed else 0, _now(), tg_id),
    )
    await db.commit()


async def complete_registration(
    tg_id: int, username: str | None, name: str, phone: str, city: str
) -> None:
    now = _now()
    db = _conn()
    await db.execute(
        """
        UPDATE users SET
            username = ?, name = ?, phone = ?, city = ?,
            subscribed = 1, registered = 1, active = 1,
            funnel_stage = 'registered', updated_at = ?
        WHERE tg_id = ?
        """,
        (username, name, phone, city, now, tg_id),
    )
    await db.commit()


async def set_funnel_stage(tg_id: int, stage: str) -> None:
    db = _conn()
    await db.execute(
        "UPDATE users SET funnel_stage = ?, updated_at = ? WHERE tg_id = ?",
        (stage, _now(), tg_id),
    )
    await db.commit()


async def set_paid(tg_id: int, paid: bool) -> None:
    db = _conn()
    stage = "paid" if paid else "registered"
    await db.execute(
        "UPDATE users SET paid = ?, funnel_stage = ?, updated_at = ? WHERE tg_id = ?",
        (1 if paid else 0, stage, _now(), tg_id),
    )
    await db.commit()


async def set_active(tg_id: int, active: bool) -> None:
    db = _conn()
    await db.execute(
        "UPDATE users SET active = ?, updated_at = ? WHERE tg_id = ?",
        (1 if active else 0, _now(), tg_id),
    )
    await db.commit()


async def all_active_users() -> list[dict[str, Any]]:
    db = _conn()
    async with db.execute(
        "SELECT * FROM users WHERE active = 1 AND registered = 1"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def stats() -> dict[str, Any]:
    db = _conn()
    out: dict[str, Any] = {}
    async with db.execute("SELECT COUNT(*) AS c FROM users") as cur:
        out["users"] = (await cur.fetchone())["c"]
    async with db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE registered = 1"
    ) as cur:
        out["registered"] = (await cur.fetchone())["c"]
    async with db.execute("SELECT COUNT(*) AS c FROM users WHERE paid = 1") as cur:
        out["paid"] = (await cur.fetchone())["c"]
    async with db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE active = 1"
    ) as cur:
        out["active"] = (await cur.fetchone())["c"]
    by_stage: dict[str, int] = {}
    async with db.execute(
        "SELECT funnel_stage, COUNT(*) AS c FROM users GROUP BY funnel_stage"
    ) as cur:
        for row in await cur.fetchall():
            by_stage[row["funnel_stage"]] = row["c"]
    out["by_stage"] = by_stage
    return out


# --------------------------------------------------------------------------- #
# Scheduled drip jobs
# --------------------------------------------------------------------------- #
async def add_job(job_id: str, tg_id: int, kind: str, run_at: int) -> None:
    """Idempotent insert (INSERT OR IGNORE on job_id PK)."""
    db = _conn()
    await db.execute(
        """
        INSERT OR IGNORE INTO scheduled_jobs (job_id, tg_id, kind, run_at, sent)
        VALUES (?, ?, ?, ?, 0)
        """,
        (job_id, tg_id, kind, run_at),
    )
    await db.commit()


async def mark_job_sent(job_id: str) -> None:
    db = _conn()
    await db.execute(
        "UPDATE scheduled_jobs SET sent = 1 WHERE job_id = ?", (job_id,)
    )
    await db.commit()


async def pending_jobs() -> list[dict[str, Any]]:
    """All not-yet-sent jobs (used to re-arm the scheduler on boot)."""
    db = _conn()
    async with db.execute(
        "SELECT * FROM scheduled_jobs WHERE sent = 0"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def cancel_jobs_for_user(tg_id: int) -> None:
    """Mark all of a user's pending jobs as sent so they are never fired."""
    db = _conn()
    await db.execute(
        "UPDATE scheduled_jobs SET sent = 1 WHERE tg_id = ? AND sent = 0",
        (tg_id,),
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# Lead retry queue
# --------------------------------------------------------------------------- #
async def enqueue_lead(tg_id: int, payload_json: str) -> None:
    db = _conn()
    await db.execute(
        """
        INSERT INTO lead_queue (tg_id, payload, attempts, created_at, next_try_at)
        VALUES (?, ?, 0, ?, ?)
        """,
        (tg_id, payload_json, _now(), _now()),
    )
    await db.commit()


async def due_leads(limit: int = 20) -> list[dict[str, Any]]:
    db = _conn()
    async with db.execute(
        "SELECT * FROM lead_queue WHERE next_try_at <= ? ORDER BY id LIMIT ?",
        (_now(), limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_lead(lead_id: int) -> None:
    db = _conn()
    await db.execute("DELETE FROM lead_queue WHERE id = ?", (lead_id,))
    await db.commit()


async def reschedule_lead(lead_id: int, attempts: int, next_try_at: int) -> None:
    db = _conn()
    await db.execute(
        "UPDATE lead_queue SET attempts = ?, next_try_at = ? WHERE id = ?",
        (attempts, next_try_at, lead_id),
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# AI: conversation memory, pause flag, notes
# --------------------------------------------------------------------------- #
async def append_message(tg_id: int, payload_json: str) -> None:
    db = _conn()
    await db.execute(
        "INSERT INTO messages (tg_id, payload, created_at) VALUES (?, ?, ?)",
        (tg_id, payload_json, _now()),
    )
    await db.commit()


async def load_messages(tg_id: int, limit: int = 80) -> list[str]:
    """Return up to `limit` payload strings, NEWEST-FIRST (caller reverses)."""
    db = _conn()
    async with db.execute(
        "SELECT payload FROM messages WHERE tg_id = ? ORDER BY id DESC LIMIT ?",
        (tg_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [r["payload"] for r in rows]


async def forget_user_messages(tg_id: int) -> None:
    db = _conn()
    await db.execute("DELETE FROM messages WHERE tg_id = ?", (tg_id,))
    await db.commit()


async def set_ai_paused(tg_id: int, paused: bool, minutes: int | None = None) -> None:
    """Pause/resume the AI for a user. minutes>0 => auto-resume after that;
    paused with no minutes => indefinite (manual /ai_off)."""
    db = _conn()
    if paused:
        until = _now() + int(minutes) * 60 if minutes else 0
        await db.execute(
            "UPDATE users SET ai_paused = 1, ai_paused_until = ?, updated_at = ? WHERE tg_id = ?",
            (until, _now(), tg_id),
        )
    else:
        await db.execute(
            "UPDATE users SET ai_paused = 0, ai_paused_until = 0, updated_at = ? WHERE tg_id = ?",
            (_now(), tg_id),
        )
    await db.commit()


async def is_ai_active(tg_id: int) -> bool:
    """True if the AI may answer this user (not paused, or pause expired)."""
    db = _conn()
    async with db.execute(
        "SELECT ai_paused, ai_paused_until FROM users WHERE tg_id = ?", (tg_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return True
    if not row["ai_paused"]:
        return True
    until = row["ai_paused_until"] or 0
    return until > 0 and _now() >= until  # expired -> active again


async def append_ai_note(tg_id: int, text: str) -> None:
    db = _conn()
    await db.execute(
        "INSERT INTO ai_notes (tg_id, text, created_at) VALUES (?, ?, ?)",
        (tg_id, text, _now()),
    )
    await db.commit()
