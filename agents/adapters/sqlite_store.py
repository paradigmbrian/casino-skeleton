from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agents.ports import EventBus
from agents.types import Event, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'unknown',
    created_at  TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_unconsumed
    ON events(created_at) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    worker      TEXT NOT NULL,
    event_json  TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'queued',
    attempts    INTEGER NOT NULL DEFAULT 0,
    leased_at   TEXT,
    created_at  TEXT NOT NULL
);
-- The dedupe rail: at most one live task per unit of work.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_inflight
    ON tasks(dedupe_key) WHERE state IN ('queued', 'leased');

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    worker        TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    branch        TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    cost_usd      REAL NOT NULL DEFAULT 0,
    num_turns     INTEGER NOT NULL DEFAULT 0,
    files_changed TEXT NOT NULL DEFAULT '[]',
    summary       TEXT NOT NULL DEFAULT '',
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteStore(EventBus):
    """All three ports over one file, because one file is easier to inspect
    live during a demo than three services."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ---------------- EventBus ----------------

    def publish(self, event: Event) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO events (id, type, payload, depth, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event.id, event.type, json.dumps(event.payload, sort_keys=True),
                 event.depth, event.source, event.created_at),
            )

    def drain(self, limit: int = 50) -> list[Event]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT * FROM events WHERE consumed_at IS NULL"
                " ORDER BY created_at, rowid LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                c.executemany(
                    "UPDATE events SET consumed_at = ? WHERE id = ?",
                    [(utcnow(), r["id"]) for r in rows],
                )
            c.execute("COMMIT")
        return [
            Event(
                id=r["id"], type=r["type"], payload=json.loads(r["payload"]),
                depth=r["depth"], source=r["source"], created_at=r["created_at"],
            )
            for r in rows
        ]
