from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agents.ports import EventBus, RunStore, WorkQueue
from agents.types import Event, RunRecord, Task, utcnow

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


class SqliteStore(EventBus, WorkQueue, RunStore):
    """All three ports over one file, because one file is easier to inspect
    live during a demo than three services."""

    def __init__(self, db_path: Path | str, ledger_path: Path | str | None = None):
        self.db_path = Path(db_path)
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
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

    # ---------------- WorkQueue ----------------

    def enqueue(self, task: Task) -> bool:
        """False when identical work is already queued or leased. The partial
        unique index on dedupe_key does the enforcing, so two orchestrator
        instances cannot race past it."""
        payload = json.dumps(
            {
                "id": task.event.id, "type": task.event.type, "payload": task.event.payload,
                "depth": task.event.depth, "source": task.event.source,
                "created_at": task.event.created_at,
            },
            sort_keys=True,
        )
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO tasks (id, worker, event_json, dedupe_key, state, attempts, created_at)"
                    " VALUES (?, ?, ?, ?, 'queued', 0, ?)",
                    (task.id, task.worker, payload, task.dedupe_key, utcnow()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        raw = json.loads(row["event_json"])
        return Task(
            id=row["id"], worker=row["worker"], dedupe_key=row["dedupe_key"],
            attempts=row["attempts"], event=Event(**raw),
        )

    def lease(self) -> Task | None:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM tasks WHERE state = 'queued' ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute(
                "UPDATE tasks SET state='leased', leased_at=?, attempts=attempts+1 WHERE id=?",
                (utcnow(), row["id"]),
            )
            updated = c.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            c.execute("COMMIT")
        return self._row_to_task(updated)

    def ack(self, task_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE tasks SET state='done' WHERE id=?", (task_id,))

    def nack(self, task_id: str, max_attempts: int) -> str:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
            attempts = row["attempts"] if row else max_attempts
            state = "dead" if attempts >= max_attempts else "queued"
            c.execute("UPDATE tasks SET state=? WHERE id=?", (state, task_id))
            c.execute("COMMIT")
        return "dead_lettered" if state == "dead" else "requeued"

    def depth(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state").fetchall()
        counts = {"queued": 0, "leased": 0, "done": 0, "dead": 0}
        counts.update({r["state"]: r["n"] for r in rows})
        return counts

    # ---------------- RunStore ----------------

    def record(self, run: RunRecord) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (run_id, worker, event_type, task_id, branch, status,"
                " started_at, ended_at, cost_usd, num_turns, files_changed, summary, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                "   status=excluded.status, ended_at=excluded.ended_at,"
                "   cost_usd=excluded.cost_usd, num_turns=excluded.num_turns,"
                "   files_changed=excluded.files_changed, summary=excluded.summary,"
                "   error=excluded.error",
                (run.run_id, run.worker, run.event_type, run.task_id, run.branch, run.status,
                 run.started_at, run.ended_at, run.cost_usd, run.num_turns,
                 json.dumps(list(run.files_changed)), run.summary, run.error),
            )
        if self.ledger_path:
            payload = {**run.__dict__, "files_changed": list(run.files_changed),
                       "logged_at": utcnow()}
            with self.ledger_path.open("a") as fh:
                fh.write(json.dumps(payload, sort_keys=True) + "\n")

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"], worker=row["worker"], event_type=row["event_type"],
            task_id=row["task_id"], branch=row["branch"], status=row["status"],
            started_at=row["started_at"], ended_at=row["ended_at"],
            cost_usd=row["cost_usd"], num_turns=row["num_turns"],
            files_changed=tuple(json.loads(row["files_changed"])),
            summary=row["summary"], error=row["error"],
        )

    def recent(self, limit: int = 20) -> list[RunRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def cost_since(self, iso_timestamp: str) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) total FROM runs WHERE started_at >= ?",
                (iso_timestamp,),
            ).fetchone()
        return float(row["total"])

    def get_meta(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
