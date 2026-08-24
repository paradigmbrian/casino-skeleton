# Autonomous Agent Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, event-driven agent layer that maintains this blackjack repo on its own — four LLM workers fired by non-human triggers, isolated in git worktrees, landing real commits on `main` only through a test-verified merge gate.

**Architecture:** Three planes. Sensors (pure Python, no LLM) publish typed events to a SQLite event bus. A deterministic orchestrator routes events to workers via a static table and enqueues deduped tasks. Workers run `claude-agent-sdk` inside their own git worktree under a per-worker tool allowlist and write-scope permission callback; a serialized merge gate verifies scope, runs the full suite, and fast-forwards `main`.

**Tech Stack:** Python 3.12 (`.venv`, uv-managed), `claude-agent-sdk` 0.2.144, `coverage`, stdlib `sqlite3` / `asyncio` / `urllib`, pytest, git worktrees.

**Spec:** `docs/superpowers/specs/2026-08-23-autonomous-agent-layer-design.md`

## Global Constraints

- **Python interpreter is `.venv/bin/python`** (3.12.10, uv-managed). The venv has no `pip`; install with `uv pip install --python .venv/bin/python <pkg>`.
- **Agent-layer dependencies go in `agents/requirements.txt`, never the root `requirements.txt`.** The root file is `dep-updater`'s write scope; the agent must not bump the SDK out from under the running layer.
- **`casino/` and `tests/` are never edited by hand during this plan.** They are the workers' territory. The only exception is Task 15's end-to-end smoke check, which reverts anything it touches.
- **Worker model is `claude-opus-5`**, `effort="high"`, `thinking={"type": "adaptive"}`. The control plane makes zero LLM calls.
- **Write scopes are disjoint and are declared as either an exact file path or a directory prefix ending in `/`.** No glob syntax — see Task 7 for why.
- **The merge gate is the only writer to `main`.** Workers commit inside their own worktree and never touch the primary checkout.
- **`setting_sources` must stay `None`** on every `ClaudeAgentOptions`, so workers do not inherit this repo's `CLAUDE.md` or the user's global settings.
- **Every test runs with `.venv/bin/python -m pytest`.** Agent-layer tests live under `tests/agents/` and must not depend on network or on a live Anthropic API key.

## File Structure

| File | Responsibility |
|---|---|
| `agents/types.py` | `Event`, `Task`, `RunRecord` dataclasses; cascade depth; dedupe keys |
| `agents/config.py` | Routing table, cadences, thresholds, budget ceilings, paths |
| `agents/ports.py` | `EventBus`, `WorkQueue`, `RunStore` ABCs — the AWS seam |
| `agents/adapters/sqlite_store.py` | Schema + all three ports over one SQLite file |
| `agents/worktree.py` | Worktree/branch create, park, cleanup |
| `agents/scope.py` | The write-scope predicate, shared by the permission guard and the gate |
| `agents/merge_gate.py` | Scope check -> integration merge -> pytest -> ff-merge -> emit `commit.pushed` |
| `agents/worker.py` | Generic runtime wrapping `claude-agent-sdk`; permission guard; ledger record |
| `agents/specs/*.py` | One `WorkerSpec` (prompt, tools, scope, limits) per worker |
| `agents/sensors/*.py` | `git`, `coverage`, `timer`, `sim_runner`, `anomaly` |
| `agents/orchestrator.py` | Router, dedupe, cascade cap, budget guard |
| `agents/supervisor.py` | asyncio: sensor loop + orchestrator loop + bounded worker pool |
| `agents/cli.py` | `up` / `status` / `events` / `stop` |
| `hooks/post-commit` | Publishes `commit.pushed` immediately |
| `docs/aws-mapping.md` | The production story |

---

### Task 1: Domain types and configuration

**Files:**
- Create: `agents/__init__.py`, `agents/types.py`, `agents/config.py`, `agents/requirements.txt`
- Create: `tests/agents/__init__.py`, `tests/agents/test_types.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Event(type, payload, depth, id, created_at, source)` with `.dedupe_key() -> str` and `.child(type, payload) -> Event`; `Task(id, worker, event, dedupe_key, attempts)`; `RunRecord`; `config.ROUTES: dict[str, str]`; `config.MAX_CASCADE_DEPTH: int`; `config.REPO_ROOT: Path`; `config.STATE_DIR: Path`; `config.DB_PATH: Path`

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_types.py
from agents.types import Event, Task, RunRecord


def test_dedupe_key_is_stable_across_identical_events():
    a = Event(type="coverage.gap", payload={"module": "casino/table.py", "pct": 0.0})
    b = Event(type="coverage.gap", payload={"pct": 0.0, "module": "casino/table.py"})
    assert a.id != b.id                      # distinct events
    assert a.dedupe_key() == b.dedupe_key()  # same logical work, key order irrelevant


def test_dedupe_key_differs_on_payload_change():
    a = Event(type="coverage.gap", payload={"module": "casino/table.py"})
    b = Event(type="coverage.gap", payload={"module": "casino/cards.py"})
    assert a.dedupe_key() != b.dedupe_key()


def test_child_increments_cascade_depth_and_keeps_source():
    root = Event(type="commit.pushed", payload={"sha": "abc"}, source="git_sensor")
    kid = root.child("review.fix_requested", {"finding": "dead code"})
    assert kid.depth == 1
    assert kid.source == "git_sensor"
    grandkid = kid.child("regression.needed", {})
    assert grandkid.depth == 2


def test_task_carries_the_event_and_its_dedupe_key():
    ev = Event(type="deps.stale", payload={"package": "requests"})
    task = Task(worker="dep-updater", event=ev, dedupe_key=ev.dedupe_key())
    assert task.attempts == 0
    assert task.event.type == "deps.stale"


def test_run_record_defaults_to_zero_cost():
    rec = RunRecord(run_id="r1", worker="reviewer", event_type="commit.pushed",
                    task_id="t1", branch="agent/review-abc", status="dispatched",
                    started_at="2026-08-23T00:00:00+00:00")
    assert rec.cost_usd == 0.0
    assert rec.files_changed == ()
    assert rec.ended_at is None
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_types.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents'`

- [x] **Step 3: Create the package and types**

```python
# agents/__init__.py
"""Autonomous agent layer that maintains this repository."""
```

```python
# agents/types.py
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Event:
    """Something a sensor observed. Immutable; cascades carry increasing depth."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    depth: int = 0
    source: str = "unknown"
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=utcnow)

    def dedupe_key(self) -> str:
        """Identity of the *work*, not of the event. Two sensors observing the
        same gap produce different ids but the same dedupe key."""
        blob = json.dumps({"type": self.type, "payload": self.payload}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def child(self, type: str, payload: dict[str, Any]) -> "Event":
        return Event(type=type, payload=payload, depth=self.depth + 1, source=self.source)


@dataclass(frozen=True)
class Task:
    worker: str
    event: Event
    dedupe_key: str
    id: str = field(default_factory=_new_id)
    attempts: int = 0


@dataclass(frozen=True)
class RunRecord:
    """One worker execution, start to finish. This is the audit trail."""

    run_id: str
    worker: str
    event_type: str
    task_id: str
    branch: str
    status: str  # dispatched|agent_done|scope_rejected|tests_failed|merged|timeout|error
    started_at: str
    ended_at: str | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    files_changed: tuple[str, ...] = ()
    summary: str = ""
    error: str | None = None
```

- [x] **Step 4: Write the config module**

```python
# agents/config.py
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "agents" / "state"
DB_PATH = STATE_DIR / "agents.db"
LEDGER_PATH = STATE_DIR / "runs.jsonl"
STOP_FLAG = STATE_DIR / "STOP"
WORKTREE_ROOT = REPO_ROOT.parent / f"{REPO_ROOT.name}-worktrees"

# Deterministic dispatch. The whole control plane is this dict.
ROUTES: dict[str, str] = {
    "commit.pushed": "reviewer",
    "review.fix_requested": "reviewer",
    "coverage.gap": "test-author",
    "test.failed": "test-author",
    "regression.needed": "test-author",
    "deps.stale": "dep-updater",
    "outcome.anomaly": "anomaly-investigator",
    "outcome.invariant_violation": "anomaly-investigator",
}

MAX_CASCADE_DEPTH = 3
MAX_CONCURRENT_WORKERS = 2
MAX_TASK_ATTEMPTS = 2

# Budget. Per-run is enforced natively by the SDK; hourly is ours.
HOURLY_BUDGET_USD = 5.00

# Sensor cadences, seconds.
GIT_POLL_S = 10
TIMER_S = 90
SIM_RUNNER_S = 120

COVERAGE_THRESHOLD = 80.0
ANOMALY_Z_THRESHOLD = 3.0
ANOMALY_BASELINE_ROUNDS = 5000
ANOMALY_BATCH_ROUNDS = 200

PUSH_ENABLED = False  # the one action that leaves this machine
MAIN_BRANCH = "main"
```

```
# agents/requirements.txt
claude-agent-sdk==0.2.144
coverage==7.6.10
```

- [x] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_types.py -q`
Expected: PASS, 5 passed

- [x] **Step 6: Install the agent-layer dependencies**

Run: `uv pip install --python .venv/bin/python -r agents/requirements.txt`
Expected: `claude-agent-sdk` and `coverage` resolve. Verify: `.venv/bin/python -c "import claude_agent_sdk, coverage; print('ok')"`

- [x] **Step 7: Ignore agent state, then commit**

Append to `.gitignore`:

```
agents/state/
.coverage
coverage.json
```

```bash
git add agents/ tests/agents/ .gitignore
git commit -m "feat(agents): domain types, routing table, and configuration"
```

---

### Task 2: SQLite schema and the EventBus port

**Files:**
- Create: `agents/ports.py`, `agents/adapters/__init__.py`, `agents/adapters/sqlite_store.py`
- Create: `tests/agents/test_event_bus.py`

**Interfaces:**
- Consumes: `agents.types.Event`, `agents.config.DB_PATH`
- Produces: `EventBus` ABC with `publish(event) -> None` and `drain(limit: int = 50) -> list[Event]`; `SqliteStore(db_path)` implementing it, plus `SqliteStore.connect()` creating the schema idempotently

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_event_bus.py
import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.types import Event


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "t.db")


def test_drain_returns_published_events_once(store):
    store.publish(Event(type="deps.stale", payload={"package": "requests"}))
    first = store.drain()
    assert [e.type for e in first] == ["deps.stale"]
    assert store.drain() == []  # consumed events are not redelivered


def test_drain_preserves_payload_depth_and_source(store):
    store.publish(Event(type="commit.pushed", payload={"sha": "abc123"}, depth=2, source="git_sensor"))
    (ev,) = store.drain()
    assert ev.payload == {"sha": "abc123"}
    assert ev.depth == 2
    assert ev.source == "git_sensor"


def test_events_survive_reopening_the_database(tmp_path):
    path = tmp_path / "t.db"
    SqliteStore(path).publish(Event(type="coverage.gap", payload={"module": "casino/table.py"}))
    assert [e.type for e in SqliteStore(path).drain()] == ["coverage.gap"]


def test_drain_respects_limit_and_is_fifo(store):
    for i in range(5):
        store.publish(Event(type="timer.tick", payload={"i": i}))
    batch = store.drain(limit=2)
    assert [e.payload["i"] for e in batch] == [0, 1]


def test_publishing_the_same_event_id_twice_is_idempotent(store):
    ev = Event(type="deps.stale", payload={"package": "requests"})
    store.publish(ev)
    store.publish(ev)
    assert len(store.drain()) == 1
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_event_bus.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.adapters'`

- [x] **Step 3: Write the port ABCs**

```python
# agents/ports.py
from __future__ import annotations

from abc import ABC, abstractmethod

from agents.types import Event, RunRecord, Task


class EventBus(ABC):
    """Local: SQLite table. AWS: EventBridge custom bus."""

    @abstractmethod
    def publish(self, event: Event) -> None: ...

    @abstractmethod
    def drain(self, limit: int = 50) -> list[Event]: ...


class WorkQueue(ABC):
    """Local: SQLite table with a partial unique index. AWS: SQS + DLQ."""

    @abstractmethod
    def enqueue(self, task: Task) -> bool:
        """False when an identical unit of work is already queued or leased."""

    @abstractmethod
    def lease(self) -> Task | None: ...

    @abstractmethod
    def ack(self, task_id: str) -> None: ...

    @abstractmethod
    def nack(self, task_id: str, max_attempts: int) -> str:
        """Returns 'requeued' or 'dead_lettered'."""

    @abstractmethod
    def depth(self) -> dict[str, int]: ...


class RunStore(ABC):
    """Local: SQLite table mirrored to JSONL. AWS: DynamoDB + CloudWatch Logs."""

    @abstractmethod
    def record(self, run: RunRecord) -> None: ...

    @abstractmethod
    def recent(self, limit: int = 20) -> list[RunRecord]: ...

    @abstractmethod
    def cost_since(self, iso_timestamp: str) -> float: ...

    @abstractmethod
    def get_meta(self, key: str) -> str | None: ...

    @abstractmethod
    def set_meta(self, key: str, value: str) -> None: ...
```

- [x] **Step 4: Implement the schema and the EventBus half**

```python
# agents/adapters/__init__.py
```

```python
# agents/adapters/sqlite_store.py
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
```

- [x] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_event_bus.py -q`
Expected: PASS, 5 passed

- [x] **Step 6: Commit**

```bash
git add agents/ports.py agents/adapters/ tests/agents/test_event_bus.py
git commit -m "feat(agents): port interfaces and SQLite-backed event bus"
```

---
### Task 3: WorkQueue with lease, dedupe, and dead-lettering

**Files:**
- Modify: `agents/adapters/sqlite_store.py` (append the WorkQueue methods to `SqliteStore`)
- Create: `tests/agents/test_work_queue.py`

**Interfaces:**
- Consumes: `agents.types.Task`, `agents.types.Event`
- Produces: `SqliteStore.enqueue(task) -> bool`, `.lease() -> Task | None`, `.ack(task_id) -> None`, `.nack(task_id, max_attempts) -> str`, `.depth() -> dict[str, int]`

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_work_queue.py
import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.types import Event, Task


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "t.db")


def make_task(module="casino/table.py", worker="test-author"):
    ev = Event(type="coverage.gap", payload={"module": module})
    return Task(worker=worker, event=ev, dedupe_key=ev.dedupe_key())


def test_enqueue_then_lease_returns_the_task(store):
    task = make_task()
    assert store.enqueue(task) is True
    leased = store.lease()
    assert leased is not None
    assert leased.id == task.id
    assert leased.worker == "test-author"
    assert leased.event.payload["module"] == "casino/table.py"


def test_lease_returns_none_on_empty_queue(store):
    assert store.lease() is None


def test_duplicate_work_is_refused_while_one_is_in_flight(store):
    assert store.enqueue(make_task()) is True
    assert store.enqueue(make_task()) is False   # same dedupe key, still queued
    store.lease()
    assert store.enqueue(make_task()) is False   # still refused while leased


def test_same_work_may_be_requeued_after_ack(store):
    first = make_task()
    store.enqueue(first)
    store.lease()
    store.ack(first.id)
    assert store.enqueue(make_task()) is True    # the earlier one is done


def test_leasing_increments_attempts(store):
    task = make_task()
    store.enqueue(task)
    assert store.lease().attempts == 1
    store.nack(task.id, max_attempts=3)
    assert store.lease().attempts == 2


def test_nack_dead_letters_once_attempts_are_exhausted(store):
    task = make_task()
    store.enqueue(task)
    store.lease()
    assert store.nack(task.id, max_attempts=1) == "dead_lettered"
    assert store.lease() is None
    assert store.depth()["dead"] == 1


def test_lease_is_fifo_across_distinct_work(store):
    store.enqueue(make_task("casino/a.py"))
    store.enqueue(make_task("casino/b.py"))
    assert store.lease().event.payload["module"] == "casino/a.py"
    assert store.lease().event.payload["module"] == "casino/b.py"
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_work_queue.py -q`
Expected: FAIL — `AttributeError: 'SqliteStore' object has no attribute 'enqueue'`

- [x] **Step 3: Append the WorkQueue methods to `SqliteStore`**

```python
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
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_work_queue.py -q`
Expected: PASS, 7 passed

- [x] **Step 5: Commit**

```bash
git add agents/adapters/sqlite_store.py tests/agents/test_work_queue.py
git commit -m "feat(agents): work queue with lease, dedupe index, and dead-lettering"
```

---

### Task 4: Run ledger

**Files:**
- Modify: `agents/adapters/sqlite_store.py` (constructor gains `ledger_path`; append RunStore methods)
- Create: `tests/agents/test_run_store.py`

**Interfaces:**
- Consumes: `agents.types.RunRecord`
- Produces: `SqliteStore(db_path, ledger_path=None)`; `.record(run)`, `.recent(limit)`, `.cost_since(iso)`, `.get_meta(key)`, `.set_meta(key, value)`

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_run_store.py
import json

import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.types import RunRecord


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "t.db", ledger_path=tmp_path / "runs.jsonl")


def rec(run_id="r1", status="merged", cost=0.10, started="2026-08-23T12:00:00+00:00"):
    return RunRecord(run_id=run_id, worker="reviewer", event_type="commit.pushed",
                     task_id="t1", branch="agent/review-abc", status=status,
                     started_at=started, cost_usd=cost)


def test_record_then_recent_round_trips(store):
    store.record(rec())
    (got,) = store.recent()
    assert got.run_id == "r1"
    assert got.status == "merged"
    assert got.cost_usd == pytest.approx(0.10)


def test_recording_the_same_run_id_updates_rather_than_duplicates(store):
    store.record(rec(status="dispatched"))
    store.record(rec(status="merged", cost=0.42))
    runs = store.recent()
    assert len(runs) == 1
    assert runs[0].status == "merged"
    assert runs[0].cost_usd == pytest.approx(0.42)


def test_recent_is_newest_first(store):
    store.record(rec(run_id="old", started="2026-08-23T10:00:00+00:00"))
    store.record(rec(run_id="new", started="2026-08-23T11:00:00+00:00"))
    assert [r.run_id for r in store.recent()] == ["new", "old"]


def test_cost_since_sums_only_runs_at_or_after_the_cutoff(store):
    store.record(rec(run_id="a", cost=1.0, started="2026-08-23T10:00:00+00:00"))
    store.record(rec(run_id="b", cost=2.0, started="2026-08-23T12:00:00+00:00"))
    assert store.cost_since("2026-08-23T11:00:00+00:00") == pytest.approx(2.0)
    assert store.cost_since("2026-08-23T09:00:00+00:00") == pytest.approx(3.0)


def test_files_changed_survives_the_round_trip(store):
    store.record(RunRecord(run_id="r2", worker="test-author", event_type="coverage.gap",
                           task_id="t2", branch="b", status="merged",
                           started_at="2026-08-23T12:00:00+00:00",
                           files_changed=("tests/test_table.py", "tests/test_cards.py")))
    (got,) = store.recent()
    assert got.files_changed == ("tests/test_table.py", "tests/test_cards.py")


def test_every_record_is_mirrored_to_the_jsonl_ledger(store, tmp_path):
    store.record(rec())
    lines = (tmp_path / "runs.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["run_id"] == "r1"


def test_meta_round_trips_and_defaults_to_none(store):
    assert store.get_meta("baseline") is None
    store.set_meta("baseline", "0.4213")
    assert store.get_meta("baseline") == "0.4213"
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_run_store.py -q`
Expected: FAIL — `TypeError: SqliteStore.__init__() got an unexpected keyword argument 'ledger_path'`

- [x] **Step 3: Widen the constructor**

Replace the existing `__init__` with:

```python
    def __init__(self, db_path: Path | str, ledger_path: Path | str | None = None):
        self.db_path = Path(db_path)
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
```

- [x] **Step 4: Append the RunStore methods**

```python
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
```

- [x] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/ -q`
Expected: PASS, all tests from Tasks 1-4

- [x] **Step 6: Commit**

```bash
git add agents/adapters/sqlite_store.py tests/agents/test_run_store.py
git commit -m "feat(agents): run ledger with JSONL mirror and cost accounting"
```

---

### Task 5: Write-scope predicate and worktree lifecycle

**Files:**
- Create: `agents/scope.py`, `agents/worktree.py`
- Create: `tests/agents/test_scope.py`, `tests/agents/test_worktree.py`, `tests/agents/conftest.py`

**Interfaces:**
- Consumes: `agents.config`
- Produces: `scope.in_scope(path: str, scope: tuple[str, ...]) -> bool`; `scope.out_of_scope(paths, scope) -> list[str]`; `worktree.Worktree(path: Path, branch: str)`; `worktree.WorktreeManager(repo_root, worktree_root, main_branch)` with `.create(name_hint) -> Worktree`, `.park(wt) -> str`, `.cleanup(wt) -> None`

- [x] **Step 1: Write the failing scope test**

```python
# tests/agents/test_scope.py
from agents.scope import in_scope, out_of_scope

TESTS_ONLY = ("tests/",)
DEPS_ONLY = ("requirements.txt", "docs/dependencies.md")


def test_directory_prefix_matches_nested_paths():
    assert in_scope("tests/test_table.py", TESTS_ONLY)
    assert in_scope("tests/unit/test_deep.py", TESTS_ONLY)


def test_directory_prefix_does_not_match_a_sibling_with_the_same_stem():
    assert not in_scope("tests_extra/test_x.py", TESTS_ONLY)


def test_exact_file_entries_match_only_themselves():
    assert in_scope("requirements.txt", DEPS_ONLY)
    assert not in_scope("agents/requirements.txt", DEPS_ONLY)


def test_source_is_out_of_scope_for_the_test_author():
    assert not in_scope("casino/table.py", TESTS_ONLY)


def test_traversal_and_absolute_paths_are_always_out_of_scope():
    assert not in_scope("../secrets.env", TESTS_ONLY)
    assert not in_scope("tests/../casino/table.py", TESTS_ONLY)
    assert not in_scope("/etc/passwd", ("/",))


def test_out_of_scope_returns_only_the_offenders():
    changed = ["tests/test_a.py", "casino/table.py", "README.md"]
    assert out_of_scope(changed, TESTS_ONLY) == ["casino/table.py", "README.md"]
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_scope.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.scope'`

- [x] **Step 3: Implement the predicate**

Glob syntax is deliberately avoided. `fnmatch` treats `*` as matching `/`, and `PurePath.full_match` only exists on 3.13+, so `tests/**` would be quietly wrong in two different directions. A scope entry is either an exact repo-relative path or a directory prefix ending in `/`.

```python
# agents/scope.py
from __future__ import annotations

from pathlib import PurePosixPath


def _normalise(path: str) -> str | None:
    """Repo-relative posix path, or None if the path escapes the repo."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute():
        return None
    parts: list[str] = []
    for part in p.parts:
        if part in (".", ""):
            continue
        if part == "..":
            return None  # never allow traversal, even if it would resolve inside
        parts.append(part)
    return "/".join(parts) if parts else None


def in_scope(path: str, scope: tuple[str, ...]) -> bool:
    norm = _normalise(path)
    if norm is None:
        return False
    for entry in scope:
        if entry.endswith("/"):
            if norm.startswith(entry):
                return True
        elif norm == entry:
            return True
    return False


def out_of_scope(paths, scope: tuple[str, ...]) -> list[str]:
    return [p for p in paths if not in_scope(p, scope)]
```

- [x] **Step 4: Write the failing worktree test**

```python
# tests/agents/conftest.py
import subprocess

import pytest


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def temp_repo(tmp_path):
    """A throwaway git repo with one commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "agent@test.local")
    git(repo, "config", "user.name", "Agent Test")
    (repo / "README.md").write_text("# temp\n")
    (repo / "casino").mkdir()
    (repo / "casino" / "hand.py").write_text("VALUE = 21\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo
```

```python
# tests/agents/test_worktree.py
from agents.worktree import WorktreeManager
from tests.agents.conftest import git


def make_manager(temp_repo, tmp_path):
    return WorktreeManager(repo_root=temp_repo, worktree_root=tmp_path / "wt", main_branch="main")


def test_create_makes_a_checkout_on_a_new_branch(temp_repo, tmp_path):
    wt = make_manager(temp_repo, tmp_path).create("reviewer")
    assert wt.path.is_dir()
    assert (wt.path / "README.md").exists()
    assert wt.branch.startswith("agent/reviewer-")
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == wt.branch


def test_two_worktrees_are_independent(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    a, b = mgr.create("reviewer"), mgr.create("test-author")
    assert a.path != b.path and a.branch != b.branch
    (a.path / "tests" / "new.py").write_text("x = 1\n")
    assert not (b.path / "tests" / "new.py").exists()


def test_park_removes_the_checkout_but_keeps_the_branch(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    (wt.path / "casino" / "hand.py").write_text("VALUE = 22\n")
    git(wt.path, "commit", "-qam", "change")
    branch = mgr.park(wt)
    assert not wt.path.exists()
    assert branch in git(temp_repo, "branch", "--list", branch)


def test_cleanup_removes_both_checkout_and_branch(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    mgr.cleanup(wt)
    assert not wt.path.exists()
    assert git(temp_repo, "branch", "--list", wt.branch) == ""


def test_cleanup_is_safe_to_call_twice(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    mgr.cleanup(wt)
    mgr.cleanup(wt)  # must not raise
```

- [x] **Step 5: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_worktree.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.worktree'`

- [x] **Step 6: Implement the worktree manager**

```python
# agents/worktree.py
from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


class WorktreeManager:
    """One isolated checkout per worker run. Workers never see the primary
    checkout, so two agents cannot collide on the working tree."""

    def __init__(self, repo_root: Path, worktree_root: Path, main_branch: str = "main"):
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root)
        self.main_branch = main_branch
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    def create(self, name_hint: str) -> Worktree:
        slug = f"{name_hint}-{uuid.uuid4().hex[:8]}"
        branch = f"agent/{slug}"
        path = self.worktree_root / slug
        run_git(self.repo_root, "worktree", "add", "-b", branch, str(path), self.main_branch)
        return Worktree(path=path, branch=branch)

    def park(self, wt: Worktree) -> str:
        """Drop the checkout, keep the branch so a human can inspect the work."""
        run_git(self.repo_root, "worktree", "remove", "--force", str(wt.path), check=False)
        if wt.path.exists():
            shutil.rmtree(wt.path, ignore_errors=True)
        run_git(self.repo_root, "worktree", "prune", check=False)
        return wt.branch

    def cleanup(self, wt: Worktree) -> None:
        self.park(wt)
        run_git(self.repo_root, "branch", "-D", wt.branch, check=False)
```

- [x] **Step 7: Run both test files and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_scope.py tests/agents/test_worktree.py -q`
Expected: PASS, 11 passed

- [x] **Step 8: Commit**

```bash
git add agents/scope.py agents/worktree.py tests/agents/test_scope.py tests/agents/test_worktree.py tests/agents/conftest.py
git commit -m "feat(agents): write-scope predicate and git worktree lifecycle"
```

---
### Task 6: Merge gate — the single writer to `main`

**Files:**
- Create: `agents/merge_gate.py`
- Create: `tests/agents/test_merge_gate.py`

**Interfaces:**
- Consumes: `agents.scope.out_of_scope`, `agents.worktree.run_git`
- Produces: `GateResult(status, changed_files, sha, detail)`; `MergeGate(repo_root, worktree_root, main_branch="main", test_cmd=None)` with `.submit(branch: str, write_scope: tuple[str, ...]) -> GateResult`. `status` is one of `merged | scope_rejected | tests_failed | merge_conflict | empty | dirty_main`.

The gate is the only code that writes to `main`, and it is called from a single serialized point in the supervisor. Its order matters: scope is checked *before* tests, because an out-of-scope diff is a policy violation regardless of whether it happens to pass.

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_merge_gate.py
import pytest

from agents.merge_gate import MergeGate
from tests.agents.conftest import git

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_bad():\n    assert False\n"


def branch_with(repo, branch, rel_path, content, message="agent change"):
    """Commit `content` at `rel_path` on a new branch off main, without
    disturbing the primary checkout."""
    git(repo, "worktree", "add", "-q", "-b", branch, str(repo.parent / branch.replace("/", "-")), "main")
    wt = repo.parent / branch.replace("/", "-")
    target = wt / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", message)
    git(repo, "worktree", "remove", "--force", str(wt))
    return branch


@pytest.fixture
def gate(temp_repo, tmp_path):
    # A test command we control, so gate tests never depend on the real suite.
    return MergeGate(repo_root=temp_repo, worktree_root=tmp_path / "integration",
                     main_branch="main",
                     test_cmd=["python", "-c",
                               "import pathlib,sys; "
                               "sys.exit(1 if 'assert False' in "
                               "''.join(p.read_text() for p in pathlib.Path('tests').rglob('*.py')) "
                               "else 0)"])


def test_in_scope_passing_change_is_merged_into_main(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-a", "tests/test_new.py", PASSING)
    result = gate.submit("agent/tests-a", write_scope=("tests/",))
    assert result.status == "merged"
    assert result.changed_files == ("tests/test_new.py",)
    assert result.sha
    assert (temp_repo / "tests" / "test_new.py").exists()


def test_out_of_scope_change_is_rejected_before_tests_run(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-b", "casino/hand.py", "VALUE = 99\n")
    result = gate.submit("agent/tests-b", write_scope=("tests/",))
    assert result.status == "scope_rejected"
    assert "casino/hand.py" in result.detail
    assert (temp_repo / "casino" / "hand.py").read_text() == "VALUE = 21\n"  # main untouched


def test_failing_tests_block_the_merge(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-c", "tests/test_broken.py", FAILING)
    result = gate.submit("agent/tests-c", write_scope=("tests/",))
    assert result.status == "tests_failed"
    assert not (temp_repo / "tests" / "test_broken.py").exists()


def test_a_branch_with_no_changes_reports_empty(temp_repo, gate):
    git(temp_repo, "branch", "agent/tests-d", "main")
    assert gate.submit("agent/tests-d", write_scope=("tests/",)).status == "empty"


def test_a_dirty_primary_checkout_blocks_merging(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-e", "tests/test_new.py", PASSING)
    (temp_repo / "README.md").write_text("# locally edited\n")
    assert gate.submit("agent/tests-e", write_scope=("tests/",)).status == "dirty_main"


def test_the_integration_worktree_is_always_cleaned_up(temp_repo, gate, tmp_path):
    branch_with(temp_repo, "agent/tests-f", "tests/test_new.py", PASSING)
    gate.submit("agent/tests-f", write_scope=("tests/",))
    leftovers = list((tmp_path / "integration").glob("*")) if (tmp_path / "integration").exists() else []
    assert leftovers == []
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_merge_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.merge_gate'`

- [x] **Step 3: Implement the gate**

```python
# agents/merge_gate.py
from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from agents.scope import out_of_scope
from agents.worktree import run_git


@dataclass(frozen=True)
class GateResult:
    status: str  # merged|scope_rejected|tests_failed|merge_conflict|empty|dirty_main
    changed_files: tuple[str, ...] = ()
    sha: str | None = None
    detail: str = ""


class MergeGate:
    """Serialized single writer to main. Verifies scope, then verifies tests,
    then fast-forwards. Never resolves conflicts -- disjoint write scopes mean
    a conflict is a bug worth surfacing, not something to paper over."""

    def __init__(self, repo_root: Path, worktree_root: Path, main_branch: str = "main",
                 test_cmd: list[str] | None = None):
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root)
        self.main_branch = main_branch
        self.test_cmd = test_cmd or [sys.executable, "-m", "pytest", "-q"]
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    def _changed_files(self, branch: str) -> tuple[str, ...]:
        out = run_git(self.repo_root, "diff", "--name-only",
                      f"{self.main_branch}...{branch}").stdout
        return tuple(line for line in out.splitlines() if line.strip())

    def _primary_is_clean(self) -> bool:
        out = run_git(self.repo_root, "status", "--porcelain", "--untracked-files=no").stdout
        return out.strip() == ""

    def submit(self, branch: str, write_scope: tuple[str, ...]) -> GateResult:
        changed = self._changed_files(branch)
        if not changed:
            return GateResult("empty", detail=f"{branch} has no changes against {self.main_branch}")

        offenders = out_of_scope(changed, write_scope)
        if offenders:
            return GateResult("scope_rejected", changed,
                              detail=f"outside write scope {write_scope}: {offenders}")

        if not self._primary_is_clean():
            return GateResult("dirty_main", changed,
                              detail="primary checkout has uncommitted changes; refusing to merge")

        integration = self.worktree_root / f"integration-{uuid.uuid4().hex[:8]}"
        try:
            run_git(self.repo_root, "worktree", "add", "--detach", str(integration), self.main_branch)
            merge = run_git(integration, "merge", "--no-edit", branch, check=False)
            if merge.returncode != 0:
                return GateResult("merge_conflict", changed, detail=merge.stderr.strip()[:800])

            proc = subprocess.run(self.test_cmd, cwd=integration, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = (proc.stdout + proc.stderr).strip()[-1500:]
                return GateResult("tests_failed", changed, detail=tail)
        finally:
            run_git(self.repo_root, "worktree", "remove", "--force", str(integration), check=False)
            if integration.exists():
                shutil.rmtree(integration, ignore_errors=True)
            run_git(self.repo_root, "worktree", "prune", check=False)

        run_git(self.repo_root, "merge", "--no-ff", "--no-edit", branch)
        sha = run_git(self.repo_root, "rev-parse", "HEAD").stdout.strip()
        return GateResult("merged", changed, sha=sha, detail=f"merged {branch}")
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_merge_gate.py -q`
Expected: PASS, 6 passed

- [x] **Step 5: Commit**

```bash
git add agents/merge_gate.py tests/agents/test_merge_gate.py
git commit -m "feat(agents): merge gate enforcing write scope then tests before main"
```

---

### Task 7: Worker runtime and the permission guard

**Files:**
- Create: `agents/worker.py`
- Create: `tests/agents/test_worker.py`

**Interfaces:**
- Consumes: `agents.scope.in_scope`, `claude_agent_sdk`
- Produces: `WorkerSpec(name, triggers, system_prompt, allowed_tools, write_scope, max_turns, timeout_s, max_cost_usd)`; `AgentOutcome(status, summary, cost_usd, num_turns, error)`; `make_scope_guard(write_scope, worktree_path) -> CanUseTool`; `async run_agent(spec, prompt, worktree_path) -> AgentOutcome`; `ensure_committed(worktree_path, message) -> bool`

**Verified SDK facts** (introspected from `claude-agent-sdk==0.2.144`, do not re-derive):

- `query(*, prompt, options=None, transport=None)` is keyword-only and returns an `AsyncIterator`.
- `ClaudeAgentOptions` fields used here: `cwd`, `system_prompt`, `allowed_tools`, `disallowed_tools`, `permission_mode`, `can_use_tool`, `max_turns`, `max_budget_usd`, `model`, `effort`, `thinking`, `setting_sources`, `stderr`.
- `can_use_tool` signature: `async (tool_name: str, tool_input: dict, context: ToolPermissionContext) -> PermissionResultAllow | PermissionResultDeny`.
- `PermissionResultAllow(updated_input=None, updated_permissions=None)`; `PermissionResultDeny(message="", interrupt=False)`.
- `ResultMessage` carries `is_error`, `num_turns`, `total_cost_usd`, `result`, `duration_ms`, `session_id`.
- `AssistantMessage.content` is a list of blocks; text blocks are `TextBlock` with `.text`.
- `setting_sources=None` (the default) means the repo's `CLAUDE.md` and user settings are **not** loaded. Keep it that way.

- [x] **Step 1: Write the failing test — the guard, with no API calls**

```python
# tests/agents/test_worker.py
import asyncio

import pytest

from agents.worker import WorkerSpec, ensure_committed, make_scope_guard
from tests.agents.conftest import git


def decide(guard, tool_name, tool_input):
    class Ctx:
        suggestions = []
    return asyncio.run(guard(tool_name, tool_input, Ctx()))


@pytest.fixture
def guard(tmp_path):
    (tmp_path / "tests").mkdir()
    return make_scope_guard(("tests/",), tmp_path)


def test_write_inside_scope_is_allowed(guard, tmp_path):
    result = decide(guard, "Write", {"file_path": str(tmp_path / "tests" / "test_new.py")})
    assert result.behavior == "allow"


def test_write_outside_scope_is_denied_with_a_useful_message(guard, tmp_path):
    result = decide(guard, "Write", {"file_path": str(tmp_path / "casino" / "table.py")})
    assert result.behavior == "deny"
    assert "casino/table.py" in result.message
    assert "write scope" in result.message


def test_edit_is_gated_the_same_way_as_write(guard, tmp_path):
    assert decide(guard, "Edit", {"file_path": str(tmp_path / "casino" / "hand.py")}).behavior == "deny"
    assert decide(guard, "Edit", {"file_path": str(tmp_path / "tests" / "t.py")}).behavior == "allow"


def test_writes_escaping_the_worktree_are_denied(guard, tmp_path):
    assert decide(guard, "Write", {"file_path": "/etc/passwd"}).behavior == "deny"
    assert decide(guard, "Write", {"file_path": str(tmp_path.parent / "elsewhere.py")}).behavior == "deny"


def test_read_only_tools_are_always_allowed(guard):
    for tool in ("Read", "Grep", "Glob", "Bash"):
        assert decide(guard, tool, {"command": "pytest -q"}).behavior == "allow"


def test_a_write_with_no_path_argument_is_denied(guard):
    assert decide(guard, "Write", {}).behavior == "deny"


def test_ensure_committed_commits_leftover_changes(temp_repo):
    (temp_repo / "tests" / "leftover.py").write_text("x = 1\n")
    assert ensure_committed(temp_repo, "chore: leftovers") is True
    assert git(temp_repo, "status", "--porcelain") == ""
    assert "leftovers" in git(temp_repo, "log", "-1", "--pretty=%s")


def test_ensure_committed_is_a_noop_on_a_clean_tree(temp_repo):
    before = git(temp_repo, "rev-parse", "HEAD")
    assert ensure_committed(temp_repo, "chore: nothing") is False
    assert git(temp_repo, "rev-parse", "HEAD") == before


def test_worker_spec_defaults_are_conservative():
    spec = WorkerSpec(name="w", triggers=("x",), system_prompt="p",
                      allowed_tools=("Read",), write_scope=("tests/",))
    assert spec.max_turns == 25
    assert spec.timeout_s == 300
    assert spec.max_cost_usd == 0.50
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.worker'`

- [x] **Step 3: Implement the runtime**

```python
# agents/worker.py
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    query,
)

from agents.scope import in_scope

LOG = logging.getLogger("agents.worker")

# Tools that mutate the filesystem. Everything else is read-only or inert.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    triggers: tuple[str, ...]
    system_prompt: str
    allowed_tools: tuple[str, ...]
    write_scope: tuple[str, ...]
    max_turns: int = 25
    timeout_s: int = 300
    max_cost_usd: float = 0.50


@dataclass(frozen=True)
class AgentOutcome:
    status: str  # agent_done|timeout|error
    summary: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    error: str | None = None


def make_scope_guard(write_scope: tuple[str, ...], worktree_path: Path | str):
    """Prevention, not detection. A write outside the worker's scope never
    reaches the filesystem -- the merge gate's identical check is the backstop
    for anything that arrives by another route (e.g. a Bash redirect)."""
    root = Path(worktree_path).resolve()

    async def guard(tool_name: str, tool_input: dict[str, Any], context):
        if tool_name not in WRITE_TOOLS:
            return PermissionResultAllow()

        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not raw:
            return PermissionResultDeny(message=f"{tool_name} called without a file path")

        try:
            rel = Path(raw).resolve().relative_to(root)
        except ValueError:
            return PermissionResultDeny(
                message=f"{raw} is outside this worker's worktree ({root}); refused"
            )

        if in_scope(rel.as_posix(), write_scope):
            return PermissionResultAllow()

        return PermissionResultDeny(
            message=(
                f"{rel.as_posix()} is outside this worker's write scope {write_scope}. "
                "Another agent owns that path. Do not attempt it again -- if the work "
                "needs to happen there, say so in your final message instead."
            )
        )

    return guard


def ensure_committed(worktree_path: Path, message: str) -> bool:
    """Commit anything the agent left uncommitted. Returns True if it committed."""
    status = subprocess.run(["git", "-C", str(worktree_path), "status", "--porcelain"],
                            capture_output=True, text=True).stdout.strip()
    if not status:
        return False
    subprocess.run(["git", "-C", str(worktree_path), "add", "-A"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(worktree_path), "commit", "-m", message], check=True,
                   capture_output=True, text=True)
    return True


async def run_agent(spec: WorkerSpec, prompt: str, worktree_path: Path,
                    model: str = "claude-opus-5", effort: str = "high") -> AgentOutcome:
    options = ClaudeAgentOptions(
        cwd=str(worktree_path),
        system_prompt=spec.system_prompt,
        allowed_tools=list(spec.allowed_tools),
        # `default` keeps can_use_tool as the sole permission authority. Nothing
        # can hang waiting for a human because the callback answers every request.
        permission_mode="default",
        can_use_tool=make_scope_guard(spec.write_scope, worktree_path),
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_cost_usd,
        model=model,
        effort=effort,
        thinking={"type": "adaptive"},
        setting_sources=None,  # do not inherit the repo's CLAUDE.md or user settings
        stderr=lambda line: LOG.debug("claude-cli: %s", line.rstrip()),
    )

    texts: list[str] = []
    result: ResultMessage | None = None

    async def drive() -> None:
        nonlocal result
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                result = msg

    try:
        await asyncio.wait_for(drive(), timeout=spec.timeout_s)
    except asyncio.TimeoutError:
        return AgentOutcome("timeout", "\n".join(texts)[-2000:],
                            error=f"exceeded {spec.timeout_s}s")
    except Exception as exc:  # SDK/CLI failures must not kill the supervisor
        LOG.exception("worker %s crashed", spec.name)
        return AgentOutcome("error", "\n".join(texts)[-2000:], error=repr(exc))

    if result is None:
        return AgentOutcome("error", "\n".join(texts)[-2000:],
                            error="agent produced no ResultMessage")

    return AgentOutcome(
        status="error" if result.is_error else "agent_done",
        summary=(result.result or "\n".join(texts))[-2000:],
        cost_usd=result.total_cost_usd or 0.0,
        num_turns=result.num_turns or 0,
        error=(result.result or "agent reported an error") if result.is_error else None,
    )
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_worker.py -q`
Expected: PASS, 9 passed

- [x] **Step 5: Verify the guard actually fires against the live SDK**

This is the one assumption unit tests cannot settle: whether the chosen `permission_mode` routes tool calls through `can_use_tool`. Verify it once, by hand, before building anything on top.

Create `scratch_guard_check.py` in the repo root:

```python
import asyncio, tempfile
from pathlib import Path
from agents.worker import WorkerSpec, run_agent

SPEC = WorkerSpec(
    name="guard-check", triggers=(), allowed_tools=("Read", "Write"), write_scope=("tests/",),
    system_prompt="You are testing a permission guard. Do exactly what you are asked, once.",
    max_turns=4, timeout_s=120, max_cost_usd=0.20,
)

async def main():
    with tempfile.TemporaryDirectory() as d:
        wt = Path(d)
        (wt / "tests").mkdir()
        (wt / "casino").mkdir()
        out = await run_agent(SPEC, "Write the text 'x = 1' to casino/blocked.py, then stop.", wt)
        print("STATUS:", out.status)
        print("BLOCKED FILE EXISTS:", (wt / "casino" / "blocked.py").exists())
        print("SUMMARY:", out.summary[:500])

asyncio.run(main())
```

Run: `.venv/bin/python scratch_guard_check.py`
Expected: `BLOCKED FILE EXISTS: False`, and the summary mentions being denied.

**If the file WAS created**, `can_use_tool` is not being consulted under `permission_mode="default"`. Fix by trying, in order: `permission_mode="dontAsk"`, then `permission_mode="acceptEdits"`, re-running the check each time. If none route through the callback, fall back to `disallowed_tools` plus the merge gate as the sole enforcement and record that in the README's "did not go as planned" section — do not silently proceed as if the guard works.

Delete `scratch_guard_check.py` when done.

- [x] **Step 6: Commit**

```bash
git add agents/worker.py tests/agents/test_worker.py
git commit -m "feat(agents): worker runtime with tool-call-time write-scope guard"
```

---
### Task 8: The four worker specs

**Files:**
- Create: `agents/specs/__init__.py`
- Create: `tests/agents/test_specs.py`

**Interfaces:**
- Consumes: `agents.worker.WorkerSpec`
- Produces: `specs.SPECS: dict[str, WorkerSpec]`; `specs.task_brief(event: Event) -> str`

Prompts are the product here. Each one names the write scope explicitly, because an agent
that understands *why* it is blocked writes a useful final message instead of thrashing
against the guard.

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_specs.py
import pytest

from agents.config import ROUTES
from agents.specs import SPECS, task_brief
from agents.types import Event


def test_every_route_target_has_a_spec():
    assert set(ROUTES.values()) <= set(SPECS)


def test_every_spec_is_reachable_from_at_least_one_route():
    assert set(SPECS) == set(ROUTES.values())


def test_spec_triggers_agree_with_the_routing_table():
    for event_type, worker in ROUTES.items():
        assert event_type in SPECS[worker].triggers, f"{worker} missing trigger {event_type}"


def test_write_scopes_are_pairwise_disjoint():
    """The core invariant: no two workers can touch the same path."""
    def collides(a, b):
        for x in a:
            for y in b:
                if x.endswith("/") and y.endswith("/") and (x.startswith(y) or y.startswith(x)):
                    return True
                if x.endswith("/") and y.startswith(x):
                    return True
                if y.endswith("/") and x.startswith(y):
                    return True
                if x == y:
                    return True
        return False

    names = sorted(SPECS)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not collides(SPECS[a].write_scope, SPECS[b].write_scope), f"{a} overlaps {b}"


def test_test_author_cannot_write_source_and_reviewer_cannot_write_tests():
    from agents.scope import in_scope
    assert not in_scope("casino/table.py", SPECS["test-author"].write_scope)
    assert not in_scope("tests/test_table.py", SPECS["reviewer"].write_scope)


def test_investigator_is_diagnose_only():
    assert SPECS["anomaly-investigator"].write_scope == ("docs/investigations/",)
    assert "Edit" not in SPECS["anomaly-investigator"].allowed_tools


def test_every_prompt_states_its_write_scope():
    for name, spec in SPECS.items():
        for entry in spec.write_scope:
            assert entry in spec.system_prompt, f"{name} prompt does not mention {entry}"


@pytest.mark.parametrize("event_type", sorted(ROUTES))
def test_task_brief_is_non_empty_for_every_routed_event(event_type):
    brief = task_brief(Event(type=event_type, payload={"module": "casino/table.py",
                                                       "sha": "abc1234", "package": "requests",
                                                       "pinned": "2.6.0", "latest": "2.32.3",
                                                       "z": 4.1, "detail": "x"}))
    assert brief.strip()
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_specs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.specs'`

- [x] **Step 3: Write the specs**

```python
# agents/specs/__init__.py
from __future__ import annotations

from agents.types import Event
from agents.worker import WorkerSpec

READ_TOOLS = ("Read", "Grep", "Glob", "Bash")

REVIEWER = WorkerSpec(
    name="reviewer",
    triggers=("commit.pushed", "review.fix_requested"),
    allowed_tools=READ_TOOLS + ("Write", "Edit"),
    write_scope=("casino/", "docs/reviews/"),
    max_turns=30,
    timeout_s=420,
    max_cost_usd=0.75,
    system_prompt="""You are the code reviewer for a small Python blackjack simulator.

A change just landed on main. Review it and, where you are confident, fix what you find.

Your write scope is casino/ and docs/reviews/. You cannot write to tests/ -- another agent
owns tests and your attempts will be denied. If a fix would require changing a test, record
that in your review instead of attempting it.

Work in this order:
1. Read the diff for the commit named in your task (git show, git diff).
2. Read enough surrounding code to judge it. Do not review the diff in isolation.
3. Write docs/reviews/<short-sha>.md. One section per finding: what, where (file:line),
   severity (high/medium/low), and why it matters.
4. If exactly one finding is both high-confidence and low-risk, fix it in casino/.
   Run `python -m pytest -q` and confirm green before committing. If it goes red, revert
   the fix and downgrade it to a documented finding.
5. Commit with a message starting "review:".

Be specific and be honest. "This change is correct" is a fine review when it is true.
Do not invent findings to look busy. Do not fix more than one thing in a single run.""",
)

TEST_AUTHOR = WorkerSpec(
    name="test-author",
    triggers=("coverage.gap", "test.failed", "regression.needed"),
    allowed_tools=READ_TOOLS + ("Write", "Edit"),
    write_scope=("tests/",),
    max_turns=30,
    timeout_s=420,
    max_cost_usd=0.75,
    system_prompt="""You are the test author for a small Python blackjack simulator.

Your write scope is tests/ and nothing else. You cannot write to casino/ and attempts will
be denied. This is deliberate: when a test fails, either your test is wrong or you have
found a real bug. Never make a failure go away by changing the code under test.

Work in this order:
1. Read the module named in your task, and the existing tests, before writing anything.
2. Write tests that assert real behaviour -- hand values, ace demotion, bust and push
   resolution, dealer draw rules, the shape of the recorded outcome. Prefer table-driven
   cases with deterministic inputs. Seed randomness with random.seed rather than asserting
   on chance.
3. Run `python -m pytest -q`. Every test you add must pass.
4. If a test you believe is correct fails, that is a finding, not a blocker. Delete the
   failing test and state the suspected bug plainly in your final message.
5. Commit with a message starting "test:".

Do not assert on implementation details that a harmless refactor would break. Do not write
a test that cannot fail.""",
)

DEP_UPDATER = WorkerSpec(
    name="dep-updater",
    triggers=("deps.stale",),
    allowed_tools=READ_TOOLS + ("Write", "Edit"),
    write_scope=("requirements.txt", "docs/dependencies.md"),
    max_turns=20,
    timeout_s=300,
    max_cost_usd=0.40,
    system_prompt="""You are the dependency maintainer for a small Python project.

Your write scope is requirements.txt and docs/dependencies.md. Nothing else -- not source,
not tests, and not agents/requirements.txt, which belongs to the running agent layer.

Your task names a package, its current pin, and the latest release on PyPI.

Work in this order:
1. Grep the codebase for real imports of that package. A dependency nothing imports is
   itself a finding worth acting on.
2. If it is unused, remove its line from requirements.txt. If it is used, bump the pin to
   the latest version.
3. Run `python -m pytest -q` and confirm green.
4. Append a dated entry to docs/dependencies.md recording package, from, to (or removed),
   and your reasoning. Create the file if it does not exist.
5. Commit with a message starting "deps:".

Never loosen a pin into a range. Never touch source or tests.""",
)

ANOMALY_INVESTIGATOR = WorkerSpec(
    name="anomaly-investigator",
    triggers=("outcome.anomaly", "outcome.invariant_violation"),
    allowed_tools=READ_TOOLS + ("Write",),
    write_scope=("docs/investigations/",),
    max_turns=25,
    timeout_s=420,
    max_cost_usd=0.60,
    system_prompt="""You are the reliability investigator for a blackjack simulator.

Your write scope is docs/investigations/ and nothing else. You cannot change code or tests.
You diagnose; other agents act on what you find. That constraint is the point -- your
output has to be good enough for someone else to act on without re-doing your work.

Your task carries a statistical signal computed from outcomes.jsonl.

Work in this order:
1. Read the signal in your task. Understand exactly what was measured.
2. Read casino/ and form a hypothesis. Look closely at Table.play_round, Hand.value, and
   how outcomes are recorded.
3. Reproduce it read-only if you can: run `python -m casino.simulate` and inspect
   outcomes.jsonl with a short throwaway script written under /tmp.
4. Write docs/investigations/<timestamp>.md covering the signal, what you checked, your
   hypothesis, your confidence, and the single most useful next action.
5. End your final message with exactly these two lines:
   HANDOFF: fix=<one sentence for the reviewer, or the word none>
   HANDOFF: test=<one sentence for the test author, or the word none>
6. Commit with a message starting "investigate:".

Distinguish a real defect from ordinary variance. "This is within normal variance" is a
correct and valuable answer when it is true.""",
)

SPECS: dict[str, WorkerSpec] = {
    s.name: s for s in (REVIEWER, TEST_AUTHOR, DEP_UPDATER, ANOMALY_INVESTIGATOR)
}


def task_brief(event: Event) -> str:
    """The per-task user message. The system prompt says how to work; this says
    what to work on."""
    p = event.payload
    match event.type:
        case "commit.pushed":
            return (f"Commit {p.get('sha', '?')} just landed on main.\n"
                    f"Subject: {p.get('subject', '(unknown)')}\n\n"
                    f"Review it.")
        case "review.fix_requested":
            return (f"An investigation asked for a fix in casino/.\n\n"
                    f"Requested: {p.get('detail', '(none given)')}\n\n"
                    f"Assess whether it is correct before acting. If you disagree, say so "
                    f"in docs/reviews/ and make no code change.")
        case "coverage.gap":
            return (f"Module {p.get('module')} is at {p.get('pct', 0):.1f}% coverage "
                    f"(threshold {p.get('threshold', 80)}%).\n"
                    f"Uncovered lines: {p.get('missing', [])}\n\n"
                    f"Write tests for it.")
        case "test.failed":
            return (f"The suite failed after a merge attempt on branch "
                    f"{p.get('branch', '?')}.\n\nOutput:\n{p.get('detail', '')}\n\n"
                    f"Diagnose it. Fix the tests if the tests are wrong; if the source is "
                    f"wrong, delete the bad test and say so.")
        case "regression.needed":
            return (f"An investigation asked for a regression test.\n\n"
                    f"Requested: {p.get('detail', '(none given)')}\n\nWrite it.")
        case "deps.stale":
            return (f"Dependency {p.get('package')} is pinned at {p.get('pinned')}; "
                    f"the latest release on PyPI is {p.get('latest')}.\n\nHandle it.")
        case "outcome.anomaly":
            return (f"Player win rate drifted from the recorded baseline.\n"
                    f"baseline={p.get('baseline_rate')} over {p.get('baseline_n')} rounds\n"
                    f"observed={p.get('observed_rate')} over {p.get('observed_n')} rounds\n"
                    f"z={p.get('z')}\n\nInvestigate.")
        case "outcome.invariant_violation":
            return (f"An impossible outcome was recorded.\n"
                    f"Violation: {p.get('kind')}\nRow: {p.get('row')}\n\nInvestigate.")
        case _:
            return f"Event {event.type} with payload {p}. Use your judgement."
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_specs.py -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add agents/specs/ tests/agents/test_specs.py
git commit -m "feat(agents): worker specs with disjoint write scopes and task briefs"
```

---

### Task 9: Orchestrator — routing, cascade cap, budget guard

**Files:**
- Create: `agents/orchestrator.py`
- Create: `tests/agents/test_orchestrator.py`

**Interfaces:**
- Consumes: `SqliteStore`, `config.ROUTES`, `config.MAX_CASCADE_DEPTH`, `config.HOURLY_BUDGET_USD`
- Produces: `Orchestrator(store, routes=ROUTES, max_depth=..., hourly_budget_usd=...)` with `.dispatch_pending() -> list[Task]` and `.budget_remaining() -> float`; module function `parse_handoffs(summary: str, parent: Event) -> list[Event]`

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_orchestrator.py
import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.orchestrator import Orchestrator, parse_handoffs
from agents.types import Event, RunRecord


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "t.db", ledger_path=tmp_path / "runs.jsonl")


@pytest.fixture
def orch(store):
    return Orchestrator(store, hourly_budget_usd=5.0)


def test_events_route_to_the_declared_worker(store, orch):
    store.publish(Event(type="deps.stale", payload={"package": "requests"}))
    store.publish(Event(type="coverage.gap", payload={"module": "casino/table.py"}))
    tasks = orch.dispatch_pending()
    assert {t.worker for t in tasks} == {"dep-updater", "test-author"}


def test_unroutable_events_are_ignored_without_raising(store, orch):
    store.publish(Event(type="something.unknown", payload={}))
    assert orch.dispatch_pending() == []


def test_duplicate_work_is_enqueued_only_once(store, orch):
    for _ in range(3):
        store.publish(Event(type="coverage.gap", payload={"module": "casino/table.py"}))
    assert len(orch.dispatch_pending()) == 1


def test_events_past_the_cascade_cap_are_dropped(store):
    orch = Orchestrator(store, max_depth=2, hourly_budget_usd=5.0)
    store.publish(Event(type="commit.pushed", payload={"sha": "a"}, depth=2))
    assert orch.dispatch_pending() == []
    store.publish(Event(type="commit.pushed", payload={"sha": "b"}, depth=1))
    assert len(orch.dispatch_pending()) == 1


def test_dispatch_stops_once_the_hourly_budget_is_spent(store):
    orch = Orchestrator(store, hourly_budget_usd=1.0)
    store.record(RunRecord(run_id="r1", worker="reviewer", event_type="commit.pushed",
                           task_id="t1", branch="b", status="merged",
                           started_at=Event(type="x").created_at, cost_usd=1.50))
    store.publish(Event(type="deps.stale", payload={"package": "requests"}))
    assert orch.dispatch_pending() == []
    assert orch.budget_remaining() == 0.0


def test_budget_exhaustion_publishes_exactly_one_notice(store):
    orch = Orchestrator(store, hourly_budget_usd=0.01)
    store.record(RunRecord(run_id="r1", worker="reviewer", event_type="commit.pushed",
                           task_id="t1", branch="b", status="merged",
                           started_at=Event(type="x").created_at, cost_usd=9.0))
    store.publish(Event(type="deps.stale", payload={"package": "a"}))
    orch.dispatch_pending()
    store.publish(Event(type="deps.stale", payload={"package": "b"}))
    orch.dispatch_pending()
    notices = [e for e in store.drain(limit=100) if e.type == "budget.exhausted"]
    assert len(notices) == 1


def test_parse_handoffs_emits_child_events_for_both_lines():
    parent = Event(type="outcome.anomaly", payload={"z": 4.0}, depth=1, source="anomaly_sensor")
    summary = ("Investigation complete.\n"
               "HANDOFF: fix=Consult Hand.is_blackjack when resolving a natural 21.\n"
               "HANDOFF: test=Assert a two-card 21 beats a drawn 21.\n")
    events = parse_handoffs(summary, parent)
    assert {e.type for e in events} == {"review.fix_requested", "regression.needed"}
    assert all(e.depth == 2 for e in events)
    assert "is_blackjack" in next(e for e in events if e.type == "review.fix_requested").payload["detail"]


def test_parse_handoffs_ignores_none_and_missing_lines():
    parent = Event(type="outcome.anomaly", payload={})
    assert parse_handoffs("HANDOFF: fix=none\nHANDOFF: test=none\n", parent) == []
    assert parse_handoffs("no handoff lines at all", parent) == []
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_orchestrator.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.orchestrator'`

- [x] **Step 3: Implement the orchestrator**

```python
# agents/orchestrator.py
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from agents.config import HOURLY_BUDGET_USD, MAX_CASCADE_DEPTH, ROUTES
from agents.ports import EventBus, RunStore, WorkQueue
from agents.types import Event, Task

LOG = logging.getLogger("agents.orchestrator")

HANDOFF_RE = re.compile(r"^HANDOFF:\s*(fix|test)\s*=\s*(.+?)\s*$", re.MULTILINE)
HANDOFF_EVENT = {"fix": "review.fix_requested", "test": "regression.needed"}


def parse_handoffs(summary: str, parent: Event) -> list[Event]:
    """Turn an investigator's HANDOFF lines into work for other agents.
    This is the cross-worker routing that makes the layer more than four cron jobs."""
    events: list[Event] = []
    for kind, detail in HANDOFF_RE.findall(summary or ""):
        if detail.strip().lower() in ("none", "n/a", "-"):
            continue
        events.append(parent.child(HANDOFF_EVENT[kind], {"detail": detail.strip()}))
    return events


class Orchestrator:
    """Deterministic control plane. Makes zero LLM calls: given the same events
    it produces the same dispatches, every time."""

    def __init__(self, store, routes: dict[str, str] | None = None,
                 max_depth: int = MAX_CASCADE_DEPTH,
                 hourly_budget_usd: float = HOURLY_BUDGET_USD):
        self.store = store  # SqliteStore satisfies EventBus + WorkQueue + RunStore
        self.routes = dict(routes or ROUTES)
        self.max_depth = max_depth
        self.hourly_budget_usd = hourly_budget_usd
        self._budget_notice_sent = False

    def _spent_this_hour(self) -> float:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        return self.store.cost_since(cutoff)

    def budget_remaining(self) -> float:
        return max(0.0, self.hourly_budget_usd - self._spent_this_hour())

    def dispatch_pending(self) -> list[Task]:
        events = self.store.drain()
        if not events:
            return []

        if self.budget_remaining() <= 0:
            if not self._budget_notice_sent:
                self.store.publish(Event(
                    type="budget.exhausted",
                    payload={"hourly_budget_usd": self.hourly_budget_usd,
                             "spent_usd": round(self._spent_this_hour(), 4)},
                    source="orchestrator",
                ))
                self._budget_notice_sent = True
            LOG.warning("budget exhausted; dropped %d event(s) without dispatching", len(events))
            return []
        self._budget_notice_sent = False

        dispatched: list[Task] = []
        for event in events:
            worker = self.routes.get(event.type)
            if worker is None:
                LOG.debug("no route for %s; ignoring", event.type)
                continue
            if event.depth >= self.max_depth:
                LOG.warning("dropping %s at cascade depth %d (cap %d)",
                            event.type, event.depth, self.max_depth)
                continue
            task = Task(worker=worker, event=event, dedupe_key=event.dedupe_key())
            if self.store.enqueue(task):
                LOG.info("dispatched %s -> %s (depth %d)", event.type, worker, event.depth)
                dispatched.append(task)
            else:
                LOG.debug("deduped %s -> %s", event.type, worker)
        return dispatched
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_orchestrator.py -q`
Expected: PASS, 8 passed

- [x] **Step 5: Commit**

```bash
git add agents/orchestrator.py tests/agents/test_orchestrator.py
git commit -m "feat(agents): deterministic orchestrator with cascade cap and budget guard"
```

---
### Task 10: Git and coverage sensors

**Files:**
- Create: `agents/sensors/__init__.py`, `agents/sensors/git.py`, `agents/sensors/coverage.py`
- Create: `tests/agents/test_sensors_git.py`, `tests/agents/test_sensors_coverage.py`

**Interfaces:**
- Consumes: `SqliteStore` (for `get_meta`/`set_meta`), `agents.worktree.run_git`
- Produces: `GitSensor(repo_root, store, main_branch="main").poll() -> list[Event]`; `CoverageSensor(repo_root, store, threshold=80.0, package="casino", runner=None).poll() -> list[Event]`

`GitSensor` skips commits whose subject starts with `review:`. That is how "the reviewer
never reviews its own commits" is implemented — a greppable convention rather than author
metadata, so it survives the commits being made by different git identities.

- [x] **Step 1: Write the failing git-sensor test**

```python
# tests/agents/test_sensors_git.py
import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.sensors.git import GitSensor
from tests.agents.conftest import git


@pytest.fixture
def sensor(temp_repo, tmp_path):
    return GitSensor(repo_root=temp_repo, store=SqliteStore(tmp_path / "t.db"), main_branch="main")


def commit(repo, subject, path="casino/hand.py", content="VALUE = 1\n"):
    (repo / path).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", subject)


def test_first_poll_records_the_baseline_and_emits_nothing(sensor):
    assert sensor.poll() == []


def test_a_new_commit_produces_one_event(temp_repo, sensor):
    sensor.poll()
    commit(temp_repo, "feat: something")
    events = sensor.poll()
    assert [e.type for e in events] == ["commit.pushed"]
    assert events[0].payload["subject"] == "feat: something"
    assert events[0].source == "git_sensor"


def test_polling_twice_does_not_re_emit(temp_repo, sensor):
    sensor.poll()
    commit(temp_repo, "feat: something")
    sensor.poll()
    assert sensor.poll() == []


def test_several_commits_emit_in_chronological_order(temp_repo, sensor):
    sensor.poll()
    commit(temp_repo, "feat: one", content="A = 1\n")
    commit(temp_repo, "feat: two", content="A = 2\n")
    assert [e.payload["subject"] for e in sensor.poll()] == ["feat: one", "feat: two"]


def test_the_reviewers_own_commits_are_skipped(temp_repo, sensor):
    sensor.poll()
    commit(temp_repo, "review: fix dead code", content="A = 3\n")
    commit(temp_repo, "test: add coverage", content="A = 4\n")
    assert [e.payload["subject"] for e in sensor.poll()] == ["test: add coverage"]
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_sensors_git.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.sensors'`

- [x] **Step 3: Implement the git sensor**

```python
# agents/sensors/__init__.py
```

```python
# agents/sensors/git.py
from __future__ import annotations

import logging
from pathlib import Path

from agents.types import Event
from agents.worktree import run_git

LOG = logging.getLogger("agents.sensors.git")

META_KEY = "git.last_sha"
SELF_AUTHORED_PREFIXES = ("review:",)  # the reviewer must not review itself


class GitSensor:
    """Push plus reconcile. The post-commit hook publishes immediately; this
    poller catches anything committed while the layer was down."""

    def __init__(self, repo_root: Path, store, main_branch: str = "main"):
        self.repo_root = Path(repo_root)
        self.store = store
        self.main_branch = main_branch

    def poll(self) -> list[Event]:
        head = run_git(self.repo_root, "rev-parse", self.main_branch).stdout.strip()
        last = self.store.get_meta(META_KEY)

        if last is None:
            self.store.set_meta(META_KEY, head)
            LOG.info("git baseline set to %s", head[:8])
            return []
        if last == head:
            return []

        out = run_git(self.repo_root, "log", "--reverse", "--pretty=%H%x1f%s",
                      f"{last}..{head}").stdout
        self.store.set_meta(META_KEY, head)

        events: list[Event] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            sha, _, subject = line.partition("\x1f")
            if subject.startswith(SELF_AUTHORED_PREFIXES):
                LOG.debug("skipping self-authored commit %s", sha[:8])
                continue
            events.append(Event(type="commit.pushed",
                                payload={"sha": sha, "short_sha": sha[:8], "subject": subject},
                                source="git_sensor"))
        return events
```

- [x] **Step 4: Write the failing coverage-sensor test**

```python
# tests/agents/test_sensors_coverage.py
import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.sensors.coverage import CoverageSensor

REPORT = {
    "files": {
        "casino/table.py": {"summary": {"percent_covered": 0.0}, "missing_lines": [11, 12, 13]},
        "casino/hand.py": {"summary": {"percent_covered": 95.0}, "missing_lines": []},
        "casino/cards.py": {"summary": {"percent_covered": 42.5}, "missing_lines": [7]},
        "tests/test_hand.py": {"summary": {"percent_covered": 100.0}, "missing_lines": []},
        "agents/worker.py": {"summary": {"percent_covered": 10.0}, "missing_lines": [1]},
    }
}


@pytest.fixture
def sensor(tmp_path):
    return CoverageSensor(repo_root=tmp_path, store=SqliteStore(tmp_path / "t.db"),
                          threshold=80.0, package="casino", runner=lambda: REPORT)


def test_only_modules_below_threshold_are_reported(sensor):
    modules = {e.payload["module"] for e in sensor.poll()}
    assert modules == {"casino/table.py", "casino/cards.py"}


def test_payload_carries_pct_threshold_and_missing_lines(sensor):
    table = next(e for e in sensor.poll() if e.payload["module"] == "casino/table.py")
    assert table.type == "coverage.gap"
    assert table.payload["pct"] == 0.0
    assert table.payload["threshold"] == 80.0
    assert table.payload["missing"] == [11, 12, 13]
    assert table.source == "coverage_sensor"


def test_files_outside_the_package_are_ignored(sensor):
    reported = {e.payload["module"] for e in sensor.poll()}
    assert "agents/worker.py" not in reported
    assert "tests/test_hand.py" not in reported


def test_a_runner_that_fails_yields_no_events_rather_than_raising(tmp_path):
    def boom():
        raise RuntimeError("coverage exploded")

    sensor = CoverageSensor(repo_root=tmp_path, store=SqliteStore(tmp_path / "t.db"),
                            runner=boom)
    assert sensor.poll() == []
```

- [x] **Step 5: Implement the coverage sensor**

```python
# agents/sensors/coverage.py
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from agents.types import Event

LOG = logging.getLogger("agents.sensors.coverage")


def default_runner(repo_root: Path, python: str = sys.executable) -> dict:
    """Run the suite under coverage and return the parsed JSON report."""
    subprocess.run([python, "-m", "coverage", "run", "-m", "pytest", "-q"],
                   cwd=repo_root, capture_output=True, text=True)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        out_path = Path(fh.name)
    subprocess.run([python, "-m", "coverage", "json", "-o", str(out_path)],
                   cwd=repo_root, capture_output=True, text=True, check=True)
    try:
        return json.loads(out_path.read_text())
    finally:
        out_path.unlink(missing_ok=True)


class CoverageSensor:
    def __init__(self, repo_root: Path, store, threshold: float = 80.0,
                 package: str = "casino", runner=None):
        self.repo_root = Path(repo_root)
        self.store = store
        self.threshold = threshold
        self.package = package
        self.runner = runner or (lambda: default_runner(self.repo_root))

    def poll(self) -> list[Event]:
        try:
            report = self.runner()
        except Exception:
            LOG.exception("coverage run failed; emitting no events this cycle")
            return []

        events: list[Event] = []
        for path, data in sorted(report.get("files", {}).items()):
            if not path.startswith(f"{self.package}/"):
                continue
            pct = float(data.get("summary", {}).get("percent_covered", 0.0))
            if pct >= self.threshold:
                continue
            events.append(Event(
                type="coverage.gap",
                payload={"module": path, "pct": pct, "threshold": self.threshold,
                         "missing": data.get("missing_lines", [])},
                source="coverage_sensor",
            ))
        return events
```

- [x] **Step 6: Run both sensor test files and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_sensors_git.py tests/agents/test_sensors_coverage.py -q`
Expected: PASS, 9 passed

- [x] **Step 7: Commit**

```bash
git add agents/sensors/ tests/agents/test_sensors_git.py tests/agents/test_sensors_coverage.py
git commit -m "feat(agents): git and coverage sensors"
```

---

### Task 11: Timer sensor with PyPI lookup

**Files:**
- Create: `agents/sensors/timer.py`
- Create: `tests/agents/test_sensors_timer.py`

**Interfaces:**
- Consumes: `agents.types.Event`
- Produces: `parse_requirements(text: str) -> dict[str, str]`; `fetch_latest(package: str, timeout: float = 10.0) -> str | None`; `TimerSensor(requirements_path, store, fetcher=None).poll() -> list[Event]`

The network call lives here, not in the agent, so the trigger fires deterministically even
if a model call flakes. `fetcher` is injected so tests never touch the network.

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_sensors_timer.py
import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.sensors.timer import TimerSensor, parse_requirements


def test_parse_requirements_reads_exact_pins():
    text = "requests==2.6.0\n# a comment\n\nflask==3.0.0\n"
    assert parse_requirements(text) == {"requests": "2.6.0", "flask": "3.0.0"}


def test_parse_requirements_ignores_ranges_and_blank_lines():
    assert parse_requirements("requests>=2.0\n\n  \nurllib3~=2.0\n") == {}


def test_parse_requirements_strips_inline_comments_and_whitespace():
    assert parse_requirements("  requests==2.6.0  # old\n") == {"requests": "2.6.0"}


@pytest.fixture
def reqs(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("requests==2.6.0\n")
    return path


def test_a_stale_pin_emits_one_event(reqs, tmp_path):
    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=lambda p: "2.32.3")
    (event,) = sensor.poll()
    assert event.type == "deps.stale"
    assert event.payload == {"package": "requests", "pinned": "2.6.0", "latest": "2.32.3"}
    assert event.source == "timer_sensor"


def test_a_current_pin_emits_nothing(reqs, tmp_path):
    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=lambda p: "2.6.0")
    assert sensor.poll() == []


def test_the_same_upgrade_is_announced_once_per_target_version(reqs, tmp_path):
    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=lambda p: "2.32.3")
    assert len(sensor.poll()) == 1
    assert sensor.poll() == []            # already announced
    sensor.fetcher = lambda p: "2.33.0"   # a newer release is new news
    assert len(sensor.poll()) == 1


def test_a_fetch_failure_is_skipped_quietly(reqs, tmp_path):
    def boom(package):
        raise OSError("no network")

    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=boom)
    assert sensor.poll() == []


def test_a_missing_requirements_file_yields_nothing(tmp_path):
    sensor = TimerSensor(tmp_path / "nope.txt", SqliteStore(tmp_path / "t.db"),
                         fetcher=lambda p: "1.0.0")
    assert sensor.poll() == []
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_sensors_timer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.sensors.timer'`

- [x] **Step 3: Implement it**

```python
# agents/sensors/timer.py
from __future__ import annotations

import json
import logging
import re
import urllib.request
from pathlib import Path

from agents.types import Event

LOG = logging.getLogger("agents.sensors.timer")

PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-+!]+)\s*$")
META_PREFIX = "deps.announced."


def parse_requirements(text: str) -> dict[str, str]:
    """Exact pins only. A range is a deliberate choice by a human; leave it alone."""
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        match = PIN_RE.match(line)
        if match:
            pins[match.group(1)] = match.group(2)
    return pins


def fetch_latest(package: str, timeout: float = 10.0) -> str | None:
    url = f"https://pypi.org/pypi/{package}/json"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)["info"]["version"]


class TimerSensor:
    def __init__(self, requirements_path: Path, store, fetcher=None):
        self.requirements_path = Path(requirements_path)
        self.store = store
        self.fetcher = fetcher or fetch_latest

    def poll(self) -> list[Event]:
        if not self.requirements_path.exists():
            return []

        events: list[Event] = []
        for package, pinned in parse_requirements(self.requirements_path.read_text()).items():
            try:
                latest = self.fetcher(package)
            except Exception as exc:
                LOG.warning("PyPI lookup for %s failed: %s", package, exc)
                continue
            if not latest or latest == pinned:
                continue

            # Announce a given upgrade once, not every 90 seconds.
            key = f"{META_PREFIX}{package}"
            if self.store.get_meta(key) == latest:
                continue
            self.store.set_meta(key, latest)

            events.append(Event(
                type="deps.stale",
                payload={"package": package, "pinned": pinned, "latest": latest},
                source="timer_sensor",
            ))
        return events
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_sensors_timer.py -q`
Expected: PASS, 8 passed

- [x] **Step 5: Sanity-check the live lookup once**

Run: `.venv/bin/python -c "from agents.sensors.timer import fetch_latest; print('requests latest:', fetch_latest('requests'))"`
Expected: a version far newer than `2.6.0`. If this fails, the network is unavailable — note it and continue; the sensor degrades to emitting nothing.

- [x] **Step 6: Commit**

```bash
git add agents/sensors/timer.py tests/agents/test_sensors_timer.py
git commit -m "feat(agents): timer sensor with deterministic PyPI version lookup"
```

---

### Task 12: Simulation runner and anomaly sensor

**Files:**
- Create: `agents/sensors/anomaly.py`, `agents/sensors/sim_runner.py`
- Create: `tests/agents/test_sensors_anomaly.py`

**Interfaces:**
- Consumes: `SqliteStore` meta storage, `outcomes.jsonl`
- Produces: `two_proportion_z(x1, n1, x2, n2) -> float`; `invariant_violations(rows) -> list[tuple[str, dict]]`; `read_tail(path, limit) -> list[dict]`; `AnomalySensor(outcomes_path, store, z_threshold=3.0, batch=200).poll() -> list[Event]`; `sim_runner.run_simulation(repo_root, rounds, python=sys.executable) -> None`

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_sensors_anomaly.py
import json

import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.sensors.anomaly import (AnomalySensor, invariant_violations, read_tail,
                                    two_proportion_z)


def row(winner="player", pv=20, dv=18):
    return {"winner": winner, "player_strategy": "basic_17", "dealer_strategy": "standard_17",
            "player_value": pv, "dealer_value": dv}


def write_rows(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# --- statistics -------------------------------------------------------------

def test_identical_proportions_give_a_zero_z():
    assert two_proportion_z(42, 100, 420, 1000) == pytest.approx(0.0, abs=1e-9)


def test_a_large_shift_gives_a_large_z():
    assert two_proportion_z(90, 100, 420, 1000) > 3.0


def test_z_is_signed_by_direction():
    assert two_proportion_z(10, 100, 420, 1000) < -3.0


def test_degenerate_samples_do_not_divide_by_zero():
    assert two_proportion_z(0, 10, 0, 10) == 0.0


# --- invariants -------------------------------------------------------------

def test_a_bust_recorded_as_a_dealer_win_is_not_a_violation():
    """Busts legitimately log raw values above 21 -- the seed data contains
    dealer_value 25. Only a *winner-conditional* impossibility counts."""
    assert invariant_violations([row(winner="dealer", pv=25, dv=19)]) == []


def test_a_player_winning_while_bust_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(winner="player", pv=23, dv=18)])]
    assert "player_won_while_bust" in kinds


def test_a_dealer_winning_while_bust_against_a_live_player_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(winner="dealer", pv=19, dv=24)])]
    assert "dealer_won_while_bust" in kinds


def test_an_unknown_winner_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(winner="house")])]
    assert "unknown_winner" in kinds


def test_an_impossible_hand_value_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(pv=3)])]
    assert "player_value_out_of_range" in kinds


def test_ordinary_rows_produce_no_violations():
    assert invariant_violations([row(), row("dealer", 18, 20), row("push", 19, 19)]) == []


# --- sensor -----------------------------------------------------------------

def test_read_tail_returns_the_last_n_rows_and_skips_bad_lines(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join([json.dumps(row(pv=i)) for i in (10, 11, 12)] + ["{not json"]) + "\n")
    assert [r["player_value"] for r in read_tail(path, 2)] == [11, 12]


def test_the_first_poll_records_a_baseline_and_emits_no_drift(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    write_rows(path, [row("player")] * 40 + [row("dealer")] * 60)
    store = SqliteStore(tmp_path / "t.db")
    sensor = AnomalySensor(path, store, batch=100)
    assert [e for e in sensor.poll() if e.type == "outcome.anomaly"] == []
    assert store.get_meta("anomaly.baseline") is not None


def test_a_large_drift_from_the_baseline_emits_an_anomaly(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    store = SqliteStore(tmp_path / "t.db")
    write_rows(path, [row("player")] * 40 + [row("dealer")] * 60)
    sensor = AnomalySensor(path, store, batch=100)
    sensor.poll()                                        # establishes the baseline
    write_rows(path, [row("player")] * 90 + [row("dealer")] * 10)
    (event,) = [e for e in sensor.poll() if e.type == "outcome.anomaly"]
    assert abs(event.payload["z"]) > 3.0
    assert event.payload["baseline_n"] == 100
    assert event.source == "anomaly_sensor"


def test_a_batch_matching_the_baseline_emits_nothing(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    store = SqliteStore(tmp_path / "t.db")
    write_rows(path, [row("player")] * 40 + [row("dealer")] * 60)
    sensor = AnomalySensor(path, store, batch=100)
    sensor.poll()
    write_rows(path, [row("player")] * 41 + [row("dealer")] * 59)
    assert [e for e in sensor.poll() if e.type == "outcome.anomaly"] == []


def test_invariant_violations_are_emitted_even_before_a_baseline_exists(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    write_rows(path, [row(winner="player", pv=25)] * 3)
    sensor = AnomalySensor(path, SqliteStore(tmp_path / "t.db"), batch=100)
    kinds = [e.payload["kind"] for e in sensor.poll() if e.type == "outcome.invariant_violation"]
    assert kinds and all(k == "player_won_while_bust" for k in kinds)


def test_a_missing_outcomes_file_yields_nothing(tmp_path):
    sensor = AnomalySensor(tmp_path / "nope.jsonl", SqliteStore(tmp_path / "t.db"))
    assert sensor.poll() == []
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_sensors_anomaly.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.sensors.anomaly'`

- [x] **Step 3: Implement the anomaly sensor**

```python
# agents/sensors/anomaly.py
from __future__ import annotations

import json
import logging
import math
from collections import deque
from pathlib import Path

from agents.types import Event

LOG = logging.getLogger("agents.sensors.anomaly")

BASELINE_KEY = "anomaly.baseline"
WINNERS = {"player", "dealer", "push"}
MIN_HAND, MAX_HAND = 4, 30  # two 2s is the floor; a bust cannot exceed 30


def two_proportion_z(x1: int, n1: int, x2: int, n2: int) -> float:
    """Standard two-proportion z. Sample 1 is the observation, sample 2 the baseline."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p_pool = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return ((x1 / n1) - (x2 / n2)) / se


def invariant_violations(rows) -> list[tuple[str, dict]]:
    """Impossibilities, not variance. Deliberately winner-conditional: a bust
    legitimately records a raw value above 21, so `value > 21` alone is normal."""
    out: list[tuple[str, dict]] = []
    for row in rows:
        winner = row.get("winner")
        pv, dv = row.get("player_value"), row.get("dealer_value")

        if winner not in WINNERS:
            out.append(("unknown_winner", row))
            continue
        for label, value in (("player_value", pv), ("dealer_value", dv)):
            if not isinstance(value, int) or not (MIN_HAND <= value <= MAX_HAND):
                out.append((f"{label}_out_of_range", row))
        if not (isinstance(pv, int) and isinstance(dv, int)):
            continue
        if winner == "player" and pv > 21:
            out.append(("player_won_while_bust", row))
        if winner == "dealer" and dv > 21 and pv <= 21:
            out.append(("dealer_won_while_bust", row))
        if winner == "push" and (pv > 21 or dv > 21):
            out.append(("push_with_a_bust", row))
    return out


def read_tail(path: Path, limit: int) -> list[dict]:
    rows: deque[dict] = deque(maxlen=limit)
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a partially written line during an append; skip it
    return list(rows)


class AnomalySensor:
    def __init__(self, outcomes_path: Path, store, z_threshold: float = 3.0, batch: int = 200):
        self.outcomes_path = Path(outcomes_path)
        self.store = store
        self.z_threshold = z_threshold
        self.batch = batch

    def poll(self) -> list[Event]:
        if not self.outcomes_path.exists():
            return []
        rows = read_tail(self.outcomes_path, self.batch)
        if not rows:
            return []

        events: list[Event] = []

        seen: set[str] = set()
        for kind, row in invariant_violations(rows):
            if kind in seen:  # one event per kind per cycle, not per row
                continue
            seen.add(kind)
            events.append(Event(type="outcome.invariant_violation",
                                payload={"kind": kind, "row": row},
                                source="anomaly_sensor"))

        wins = sum(1 for r in rows if r.get("winner") == "player")
        raw = self.store.get_meta(BASELINE_KEY)
        if raw is None:
            self.store.set_meta(BASELINE_KEY, json.dumps({"wins": wins, "n": len(rows)}))
            LOG.info("anomaly baseline recorded: %d/%d player wins", wins, len(rows))
            return events

        baseline = json.loads(raw)
        z = two_proportion_z(wins, len(rows), baseline["wins"], baseline["n"])
        if abs(z) >= self.z_threshold:
            events.append(Event(
                type="outcome.anomaly",
                payload={"z": round(z, 3),
                         "observed_rate": round(wins / len(rows), 4), "observed_n": len(rows),
                         "baseline_rate": round(baseline["wins"] / baseline["n"], 4),
                         "baseline_n": baseline["n"]},
                source="anomaly_sensor",
            ))
        return events
```

- [x] **Step 4: Implement the simulation runner**

```python
# agents/sensors/sim_runner.py
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

LOG = logging.getLogger("agents.sensors.sim_runner")


def run_simulation(repo_root: Path, rounds: int, python: str = sys.executable) -> bool:
    """Keep fresh outcomes arriving so the anomaly sensor has something to read.
    Never raises -- a broken simulator is itself a signal, not a supervisor crash."""
    proc = subprocess.run(
        [python, "-c", f"from casino.simulate import run; run({int(rounds)})"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        LOG.warning("simulation failed (%d): %s", proc.returncode, proc.stderr.strip()[-400:])
        return False
    return True
```

- [x] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_sensors_anomaly.py -q`
Expected: PASS, 15 passed

- [x] **Step 6: Establish the real baseline against the actual simulator**

Run:
```bash
.venv/bin/python -c "
from agents.sensors.sim_runner import run_simulation
from agents.sensors.anomaly import read_tail
from agents.config import REPO_ROOT
run_simulation(REPO_ROOT, 5000)
rows = read_tail(REPO_ROOT / 'outcomes.jsonl', 5000)
wins = sum(1 for r in rows if r['winner'] == 'player')
print(f'player win rate: {wins/len(rows):.4f} over {len(rows)} rounds')
"
```
Expected: a plausible rate (roughly 0.38–0.45 for hit-to-17 versus hit-to-17). Record the
observed figure in the README. **If invariant violations appear here, that is a genuine
find — note it; the investigator will have real work on its first tick.**

- [x] **Step 7: Commit**

```bash
git add agents/sensors/anomaly.py agents/sensors/sim_runner.py tests/agents/test_sensors_anomaly.py
git commit -m "feat(agents): anomaly sensor with winner-conditional invariants and drift z-test"
```

---
### Task 13: Supervisor — the loops that tie it together

**Files:**
- Create: `agents/supervisor.py`
- Create: `tests/agents/test_supervisor.py`

**Interfaces:**
- Consumes: everything built so far
- Produces: `Supervisor(store, orchestrator, gate, worktrees, specs, repo_root, agent_runner=None)` with `async .execute(task) -> RunRecord`, `async .run()`, `.request_stop()`; `build_default_supervisor() -> Supervisor`

`execute` is the interesting method and the one under test. `run` is loop plumbing around it.
The gate is behind an `asyncio.Lock`, which is what "single writer, strictly serialized" means
in practice.

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_supervisor.py
import asyncio

import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.merge_gate import GateResult
from agents.orchestrator import Orchestrator
from agents.specs import SPECS
from agents.supervisor import Supervisor
from agents.types import Event, Task
from agents.worker import AgentOutcome
from agents.worktree import WorktreeManager


class FakeGate:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def submit(self, branch, write_scope):
        self.calls.append((branch, write_scope))
        return self.result


def build(tmp_path, temp_repo, gate, outcome):
    store = SqliteStore(tmp_path / "t.db", ledger_path=tmp_path / "runs.jsonl")
    worktrees = WorktreeManager(temp_repo, tmp_path / "wt", "main")

    async def fake_runner(spec, prompt, worktree_path):
        (worktree_path / "tests").mkdir(exist_ok=True)
        (worktree_path / "tests" / "test_agent.py").write_text("def test_x():\n    assert True\n")
        return outcome

    sup = Supervisor(store=store, orchestrator=Orchestrator(store), gate=gate,
                     worktrees=worktrees, specs=SPECS, repo_root=temp_repo,
                     agent_runner=fake_runner)
    return store, sup


def a_task(worker="test-author", event_type="coverage.gap"):
    ev = Event(type=event_type, payload={"module": "casino/table.py"})
    return Task(worker=worker, event=ev, dedupe_key=ev.dedupe_key())


OK = AgentOutcome("agent_done", summary="wrote tests", cost_usd=0.12, num_turns=6)


def test_a_merged_run_is_recorded_with_cost_and_files(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", ("tests/test_agent.py",), sha="deadbeef"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    rec = asyncio.run(sup.execute(a_task()))
    assert rec.status == "merged"
    assert rec.cost_usd == pytest.approx(0.12)
    assert rec.num_turns == 6
    assert rec.files_changed == ("tests/test_agent.py",)
    assert store.recent()[0].status == "merged"


def test_the_gate_is_called_with_the_workers_own_write_scope(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    asyncio.run(sup.execute(a_task()))
    assert gate.calls[0][1] == SPECS["test-author"].write_scope


def test_a_scope_rejection_is_recorded_and_the_branch_is_parked(tmp_path, temp_repo):
    gate = FakeGate(GateResult("scope_rejected", ("casino/x.py",), detail="outside scope"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    rec = asyncio.run(sup.execute(a_task()))
    assert rec.status == "scope_rejected"
    assert "outside scope" in (rec.error or "")


def test_failing_tests_emit_a_test_failed_event_for_the_test_author(tmp_path, temp_repo):
    gate = FakeGate(GateResult("tests_failed", ("tests/t.py",), detail="1 failed"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    asyncio.run(sup.execute(a_task()))
    types = [e.type for e in store.drain(limit=50)]
    assert "test.failed" in types


def test_a_merged_run_emits_nothing_itself_and_leaves_the_hook_to_the_git_sensor(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", ("tests/t.py",), sha="abc"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    asyncio.run(sup.execute(a_task()))
    assert [e.type for e in store.drain(limit=50)] == []


def test_investigator_handoffs_become_events_for_other_workers(tmp_path, temp_repo):
    outcome = AgentOutcome("agent_done", cost_usd=0.2, num_turns=4, summary=(
        "Done.\nHANDOFF: fix=Use Hand.is_blackjack when resolving a natural.\n"
        "HANDOFF: test=Assert a natural beats a drawn 21.\n"))
    gate = FakeGate(GateResult("merged", ("docs/investigations/x.md",), sha="abc"))
    store, sup = build(tmp_path, temp_repo, gate, outcome)
    asyncio.run(sup.execute(a_task(worker="anomaly-investigator", event_type="outcome.anomaly")))
    types = {e.type for e in store.drain(limit=50)}
    assert types == {"review.fix_requested", "regression.needed"}


def test_a_timed_out_agent_is_recorded_and_does_not_reach_the_gate(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, AgentOutcome("timeout", error="exceeded 300s"))
    rec = asyncio.run(sup.execute(a_task()))
    assert rec.status == "timeout"
    assert gate.calls == []


def test_request_stop_sets_the_flag_the_loop_checks(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    assert sup.stopping is False
    sup.request_stop()
    assert sup.stopping is True
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_supervisor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.supervisor'`

- [x] **Step 3: Implement the supervisor**

```python
# agents/supervisor.py
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from agents import config
from agents.adapters.sqlite_store import SqliteStore
from agents.merge_gate import MergeGate
from agents.orchestrator import Orchestrator, parse_handoffs
from agents.sensors.anomaly import AnomalySensor
from agents.sensors.coverage import CoverageSensor
from agents.sensors.git import GitSensor
from agents.sensors.sim_runner import run_simulation
from agents.sensors.timer import TimerSensor
from agents.specs import SPECS, task_brief
from agents.types import RunRecord, Task, utcnow
from agents.worker import ensure_committed, run_agent
from agents.worktree import WorktreeManager

LOG = logging.getLogger("agents.supervisor")


class Supervisor:
    def __init__(self, store, orchestrator, gate, worktrees, specs, repo_root: Path,
                 agent_runner=None, sensors=None, max_concurrency: int = 2):
        self.store = store
        self.orchestrator = orchestrator
        self.gate = gate
        self.worktrees = worktrees
        self.specs = specs
        self.repo_root = Path(repo_root)
        self.agent_runner = agent_runner or run_agent
        self.sensors = sensors or []
        self.max_concurrency = max_concurrency
        self.stopping = False
        self._gate_lock = asyncio.Lock()  # the single-writer guarantee

    def request_stop(self) -> None:
        self.stopping = True

    # ------------------------------------------------------------------ run one

    async def execute(self, task: Task) -> RunRecord:
        spec = self.specs[task.worker]
        run_id = uuid.uuid4().hex[:12]
        wt = self.worktrees.create(task.worker)

        record = RunRecord(run_id=run_id, worker=spec.name, event_type=task.event.type,
                           task_id=task.id, branch=wt.branch, status="dispatched",
                           started_at=utcnow())
        self.store.record(record)
        LOG.info("[%s] %s starting on %s", run_id, spec.name, wt.branch)

        outcome = await self.agent_runner(spec, task_brief(task.event), wt.path)

        def finish(status: str, files=(), error=None) -> RunRecord:
            final = RunRecord(
                run_id=run_id, worker=spec.name, event_type=task.event.type, task_id=task.id,
                branch=wt.branch, status=status, started_at=record.started_at,
                ended_at=utcnow(), cost_usd=outcome.cost_usd, num_turns=outcome.num_turns,
                files_changed=tuple(files), summary=outcome.summary, error=error,
            )
            self.store.record(final)
            LOG.info("[%s] %s -> %s ($%.4f, %d turns)", run_id, spec.name, status,
                     outcome.cost_usd, outcome.num_turns)
            return final

        if outcome.status != "agent_done":
            self.worktrees.park(wt)
            return finish(outcome.status, error=outcome.error)

        # Cross-worker handoffs are published whether or not the merge succeeds:
        # the investigator's value is the finding, not the file it wrote.
        for event in parse_handoffs(outcome.summary, task.event):
            self.store.publish(event)

        await asyncio.to_thread(ensure_committed, wt.path,
                                f"{spec.name}: automated change ({task.event.type})")

        async with self._gate_lock:
            result = await asyncio.to_thread(self.gate.submit, wt.branch, spec.write_scope)

        if result.status == "merged":
            self.worktrees.cleanup(wt)
            return finish("merged", result.changed_files)

        self.worktrees.park(wt)
        if result.status == "tests_failed":
            self.store.publish(task.event.child(
                "test.failed", {"branch": wt.branch, "detail": result.detail}))
        return finish(result.status, result.changed_files, error=result.detail)

    # ------------------------------------------------------------------ loops

    async def _sensor_loop(self) -> None:
        clocks = {id(s): 0.0 for s, _ in self.sensors}
        while not self.stopping:
            now = asyncio.get_running_loop().time()
            for sensor, interval in self.sensors:
                if now - clocks[id(sensor)] < interval:
                    continue
                clocks[id(sensor)] = now
                try:
                    events = await asyncio.to_thread(sensor.poll)
                except Exception:
                    LOG.exception("sensor %s failed", type(sensor).__name__)
                    continue
                for event in events:
                    self.store.publish(event)
                if events:
                    LOG.info("%s produced %d event(s)", type(sensor).__name__, len(events))
            await asyncio.sleep(1.0)

    async def _sim_loop(self) -> None:
        while not self.stopping:
            await asyncio.to_thread(run_simulation, self.repo_root, config.ANOMALY_BATCH_ROUNDS)
            await asyncio.sleep(config.SIM_RUNNER_S)

    async def _dispatch_loop(self) -> None:
        while not self.stopping:
            try:
                self.orchestrator.dispatch_pending()
            except Exception:
                LOG.exception("dispatch failed")
            await asyncio.sleep(2.0)

    async def _worker_loop(self, slot: int) -> None:
        while not self.stopping:
            task = await asyncio.to_thread(self.store.lease)
            if task is None:
                await asyncio.sleep(2.0)
                continue
            try:
                record = await self.execute(task)
                if record.status == "merged":
                    self.store.ack(task.id)
                else:
                    LOG.warning("task %s ended %s; nacking", task.id, record.status)
                    self.store.nack(task.id, config.MAX_TASK_ATTEMPTS)
            except Exception:
                LOG.exception("worker slot %d crashed on task %s", slot, task.id)
                self.store.nack(task.id, config.MAX_TASK_ATTEMPTS)

    async def run(self) -> None:
        LOG.info("supervisor up: %d worker slot(s), %d sensor(s)",
                 self.max_concurrency, len(self.sensors))
        coros = [self._sensor_loop(), self._sim_loop(), self._dispatch_loop()]
        coros += [self._worker_loop(i) for i in range(self.max_concurrency)]
        stop_watch = asyncio.create_task(self._watch_stop_flag())
        try:
            await asyncio.gather(*coros)
        finally:
            stop_watch.cancel()
            LOG.info("supervisor down")

    async def _watch_stop_flag(self) -> None:
        while not self.stopping:
            if config.STOP_FLAG.exists():
                LOG.info("stop flag seen; draining")
                self.request_stop()
            await asyncio.sleep(1.0)


def build_default_supervisor() -> Supervisor:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.STOP_FLAG.unlink(missing_ok=True)

    store = SqliteStore(config.DB_PATH, ledger_path=config.LEDGER_PATH)
    return Supervisor(
        store=store,
        orchestrator=Orchestrator(store),
        gate=MergeGate(config.REPO_ROOT, config.WORKTREE_ROOT / "integration", config.MAIN_BRANCH),
        worktrees=WorktreeManager(config.REPO_ROOT, config.WORKTREE_ROOT, config.MAIN_BRANCH),
        specs=SPECS,
        repo_root=config.REPO_ROOT,
        max_concurrency=config.MAX_CONCURRENT_WORKERS,
        sensors=[
            (GitSensor(config.REPO_ROOT, store, config.MAIN_BRANCH), config.GIT_POLL_S),
            (TimerSensor(config.REPO_ROOT / "requirements.txt", store), config.TIMER_S),
            (CoverageSensor(config.REPO_ROOT, store, config.COVERAGE_THRESHOLD), config.TIMER_S),
            (AnomalySensor(config.REPO_ROOT / "outcomes.jsonl", store,
                           config.ANOMALY_Z_THRESHOLD, config.ANOMALY_BATCH_ROUNDS),
             config.SIM_RUNNER_S),
        ],
    )
```

- [x] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/agents/test_supervisor.py -q`
Expected: PASS, 8 passed

- [x] **Step 5: Commit**

```bash
git add agents/supervisor.py tests/agents/test_supervisor.py
git commit -m "feat(agents): supervisor with serialized merge gate and bounded worker pool"
```

---

### Task 14: CLI

**Files:**
- Create: `agents/cli.py`, `agents/__main__.py`
- Create: `tests/agents/test_cli.py`

**Interfaces:**
- Consumes: `Supervisor`, `SqliteStore`, `config`
- Produces: `main(argv: list[str] | None = None) -> int`; `format_status(store) -> str`; `format_events(store, limit) -> str`

- [x] **Step 1: Write the failing test**

```python
# tests/agents/test_cli.py
from agents.adapters.sqlite_store import SqliteStore
from agents.cli import format_status
from agents.types import Event, RunRecord, Task


def test_status_reports_queue_depth_runs_and_spend(tmp_path):
    store = SqliteStore(tmp_path / "t.db", ledger_path=tmp_path / "runs.jsonl")
    ev = Event(type="coverage.gap", payload={"module": "casino/table.py"})
    store.enqueue(Task(worker="test-author", event=ev, dedupe_key=ev.dedupe_key()))
    store.record(RunRecord(run_id="r1", worker="reviewer", event_type="commit.pushed",
                           task_id="t1", branch="agent/review-x", status="merged",
                           started_at="2026-08-23T12:00:00+00:00", cost_usd=0.25,
                           files_changed=("casino/hand.py",)))
    out = format_status(store)
    assert "queued=1" in out
    assert "reviewer" in out
    assert "merged" in out
    assert "0.25" in out


def test_status_is_readable_when_nothing_has_happened(tmp_path):
    out = format_status(SqliteStore(tmp_path / "t.db"))
    assert "queued=0" in out
    assert "no runs yet" in out.lower()
```

- [x] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/agents/test_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.cli'`

- [x] **Step 3: Implement the CLI**

```python
# agents/cli.py
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from agents import config
from agents.adapters.sqlite_store import SqliteStore
from agents.supervisor import build_default_supervisor


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )


def format_status(store: SqliteStore) -> str:
    depth = store.depth()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    lines = [
        "queue:  " + "  ".join(f"{k}={v}" for k, v in sorted(depth.items())),
        f"spend:  ${store.cost_since(cutoff):.2f} in the last hour "
        f"(ceiling ${config.HOURLY_BUDGET_USD:.2f})",
        "",
    ]
    runs = store.recent(15)
    if not runs:
        lines.append("No runs yet.")
        return "\n".join(lines)

    lines.append(f"{'STARTED':<21} {'WORKER':<22} {'STATUS':<15} {'COST':>7}  FILES")
    for r in runs:
        files = ", ".join(r.files_changed[:3]) or "-"
        lines.append(f"{r.started_at[:19]:<21} {r.worker:<22} {r.status:<15} "
                     f"{r.cost_usd:>7.2f}  {files}")
    return "\n".join(lines)


def format_events(store: SqliteStore, limit: int) -> str:
    with store._conn() as c:
        rows = c.execute(
            "SELECT created_at, type, source, depth, consumed_at, payload FROM events"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    out = [f"{'WHEN':<21} {'EVENT':<28} {'SOURCE':<16} {'D':<2} {'STATE':<9} PAYLOAD"]
    for r in reversed(rows):
        state = "consumed" if r["consumed_at"] else "pending"
        payload = json.dumps(json.loads(r["payload"]), sort_keys=True)[:70]
        out.append(f"{r['created_at'][:19]:<21} {r['type']:<28} {r['source']:<16} "
                   f"{r['depth']:<2} {state:<9} {payload}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents", description="Autonomous agent layer")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="run the supervisor until stopped")
    sub.add_parser("status", help="queue depth, recent runs, spend")
    events = sub.add_parser("events", help="recent events")
    events.add_argument("--limit", type=int, default=30)
    events.add_argument("--follow", action="store_true")
    sub.add_parser("stop", help="ask a running supervisor to drain and exit")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "up":
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        supervisor = build_default_supervisor()
        try:
            asyncio.run(supervisor.run())
        except KeyboardInterrupt:
            supervisor.request_stop()
            print("\nstopping (in-flight work will finish)", file=sys.stderr)
        return 0

    if args.command == "stop":
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.STOP_FLAG.write_text("stop\n")
        print(f"stop flag written to {config.STOP_FLAG}")
        return 0

    store = SqliteStore(config.DB_PATH, ledger_path=config.LEDGER_PATH)
    if args.command == "status":
        print(format_status(store))
        return 0

    if args.command == "events":
        if not args.follow:
            print(format_events(store, args.limit))
            return 0
        import time
        seen = 0
        try:
            while True:
                text = format_events(store, args.limit)
                if len(text) != seen:
                    print("\033[2J\033[H" + text)
                    seen = len(text)
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

```python
# agents/__main__.py
from agents.cli import main

raise SystemExit(main())
```

- [x] **Step 4: Run the tests and check the CLI by hand**

Run: `.venv/bin/python -m pytest tests/agents/test_cli.py -q`
Expected: PASS, 2 passed

Run: `.venv/bin/python -m agents.cli status`
Expected: an empty-but-readable status table, exit 0

- [x] **Step 5: Commit**

```bash
git add agents/cli.py agents/__main__.py tests/agents/test_cli.py
git commit -m "feat(agents): CLI with up, status, events, and stop"
```

---

### Task 15: Git hook, documentation, and the end-to-end run

**Files:**
- Create: `hooks/post-commit`, `docs/aws-mapping.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything
- Produces: a running system

- [x] **Step 1: Write the post-commit hook**

```bash
# hooks/post-commit
#!/usr/bin/env bash
# Publishes commit.pushed the instant a commit lands, so the reviewer wakes in
# under a second. The GitSensor poller reconciles anything this misses (for
# example commits made while the layer was down); duplicate delivery is harmless
# because the dedupe key is the SHA.
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
exec "$REPO_ROOT/.venv/bin/python" - <<'PY'
import subprocess
from agents.adapters.sqlite_store import SqliteStore
from agents.config import DB_PATH, LEDGER_PATH
from agents.types import Event

sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
subject = subprocess.run(["git", "log", "-1", "--pretty=%s"], capture_output=True, text=True).stdout.strip()
if not subject.startswith("review:"):
    SqliteStore(DB_PATH, ledger_path=LEDGER_PATH).publish(
        Event(type="commit.pushed",
              payload={"sha": sha, "short_sha": sha[:8], "subject": subject},
              source="post_commit_hook")
    )
PY
```

- [x] **Step 2: Install it and verify it fires**

```bash
chmod +x hooks/post-commit
ln -sf ../../hooks/post-commit .git/hooks/post-commit
git commit --allow-empty -m "chore: verify post-commit hook"
.venv/bin/python -m agents.cli events --limit 5
```
Expected: a `commit.pushed` event with `source=post_commit_hook`.

**Note:** `.git/hooks/` is not tracked by git, so the symlink is a setup step, not a commit.
The README must say so.

- [x] **Step 3: Write `docs/aws-mapping.md`**

Content must cover, at minimum: the component-by-component table from spec section 8; why
workers are Fargate rather than Lambda (git, writable filesystem, multi-minute runs versus
Lambda's 15-minute ceiling and ephemeral `/tmp`); and the merge-gate serialization problem —
`desired_count = 1` is not a hard singleton during a rolling deploy, so the correct
production answer is a conditional-write lock in DynamoDB with a TTL. State plainly that
nothing here was deployed.

- [x] **Step 4: Rewrite `README.md`**

Required sections, per requirement R5:

1. **What this is** — one paragraph: the casino plus an autonomous maintenance layer.
2. **Running it** — `uv pip install --python .venv/bin/python -r agents/requirements.txt`,
   the `ln -sf` hook install, `export ANTHROPIC_API_KEY=...`, `python -m agents.cli up`,
   and the `status` / `events` / `stop` commands.
3. **The agents and their triggers** — a table of the four workers, their trigger event,
   their write scope, and one line on what they do.
4. **How it works** — the three-plane diagram, the merge gate, and the disjoint-write-scope
   invariant with the reason for it.
5. **Transparency** — where the ledger lives (`agents/state/runs.jsonl`, `agents.db`) and how
   to read it after the fact.
6. **AI tools used** — Claude Code for building this (directed and reviewed by a human);
   `claude-agent-sdk` with `claude-opus-5` as the workers' runtime.
7. **What did not go as planned / what I would improve** — fill in honestly at the end. Seed
   it with the known items: the AWS mapping is documentation and was never deployed; the
   organic anomaly path depends on the reviewer choosing the `is_blackjack` finding, so
   `--inject-anomaly` exists as a labelled fallback; and whatever the Task 7 Step 5 guard
   verification turned up.

- [x] **Step 5: Run the whole suite and confirm it is green**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: every test passes — the original 3 casino tests plus the agent-layer tests.

- [x] **Step 6: Commit the docs and hook**

```bash
git add hooks/ docs/aws-mapping.md README.md
git commit -m "docs: README, AWS mapping, and post-commit hook"
```

- [ ] **Step 7: End-to-end unattended run** — *run once on 2026-08-23 and stopped
  early. One reviewer run merged (`docs/reviews/dd43412.md`, $0.72, 18 turns) and the
  cascade was observed, but the run exposed a budget-accounting defect (see Execution
  log) and was stopped rather than left spending against a ceiling that could not trip.
  Re-run after the fixes to complete this step.*

```bash
export ANTHROPIC_API_KEY=...   # or rely on an `ant auth login` profile
.venv/bin/python -m agents.cli up -v
```

Watch for, in a second terminal, `watch -n5 '.venv/bin/python -m agents.cli status'`:

1. Within ~90s, `timer_sensor` emits `deps.stale` for `requests` and `dep-updater` commits.
2. `coverage_sensor` emits gaps for the five uncovered modules; `test-author` starts landing tests.
3. Each merge triggers `git_sensor`, which wakes `reviewer` — the visible cascade.
4. `sim_runner` keeps `outcomes.jsonl` growing; the anomaly baseline is recorded on the first pass.

Confirm afterwards:

```bash
git log --oneline | head -20        # agent commits above the template's Initial commit
.venv/bin/python -m agents.cli status
cat agents/state/runs.jsonl | tail -5
```

- [ ] **Step 8: Decide on pushing** — *not done; `PUSH_ENABLED` is still `False`.*

If the agents' commits should appear on GitHub for the submission, set `PUSH_ENABLED = True`
in `agents/config.py` or push by hand:

```bash
git push origin main
```

This is the only action that leaves the machine. Confirm the repo is private and the
interviewers are collaborators first.

---

## Self-Review

Checked after writing, against `docs/superpowers/specs/2026-08-23-autonomous-agent-layer-design.md`.

**Spec coverage:**

| Spec section | Covered by |
|---|---|
| 3 — Architecture, routing table | Tasks 1, 9 |
| 4 — Worker contracts, disjoint scopes, gate enforcement | Tasks 5, 6, 7, 8 |
| 4 — Investigator is diagnose-only, emits handoffs | Task 8 (prompt), Task 9 (`parse_handoffs`), Task 13 |
| 5 — All five sensors, hook plus reconcile, PyPI in the sensor | Tasks 10, 11, 12, 15 |
| 5 — Winner-conditional invariants, empirical baseline, z-test | Task 12 |
| 6 — Cascade cap, budget guard, timeouts, DLQ, idempotency, kill switch, scope enforcement, push flag, secret hygiene | Tasks 1, 3, 6, 7, 9, 13, 14 |
| 7 — Repo layout, `agents/requirements.txt` separation, runner surface | Tasks 1, 14 |
| 8 — Local-only; ports without cloud adapters; AWS mapping doc | Tasks 2, 15 |
| 10 — Success criteria | Task 15 Step 7 |

**Two gaps found and closed inline:**

- `--inject-anomaly` appears in the spec as a labelled demo fallback but had no task. It is now
  called out in Task 15 Step 4 as a README item. It is deliberately *not* implemented as code:
  if the organic path fires, it is dead weight, and the cut list ranks it below everything else.
  Implement it only if the recording needs it.
- Spec section 6 promises secret redaction in the ledger. Nothing in the plan writes a secret
  to the ledger — `RunRecord` carries only status, cost, file paths, and the agent's summary —
  so there is no redaction step to add. The guarantee holds by construction rather than by code.

**Placeholder scan:** clean. Every code step carries runnable code; every test step carries real
assertions. The one deliberately open item is Task 7 Step 5, which is a *verification* with a
documented decision tree, not a placeholder.

**Type consistency:** `Event`, `Task`, `RunRecord`, `WorkerSpec`, `AgentOutcome`, and `GateResult`
field names were cross-checked across all fifteen tasks. `GateResult.status` values are used
identically in Tasks 6 and 13. `SqliteStore` satisfies all three ports and is passed as one
object throughout, which the `Orchestrator` and `Supervisor` constructors both rely on.

**Ordering note for the executor:** Tasks 1–14 are strictly sequential — each depends on the one
before. Task 7 Step 5 is the only step requiring a live API key; if none is available, mark it
blocked and continue, but do not claim the guard works.


---

## Execution log

Written after implementing the plan, recording where reality diverged from it. Every item
below is a defect **in the plan as written**, found while executing it; each is fixed in the
code and described in the README's "what did not go as planned" section.

### Found by unit tests, during implementation

| # | Task | Defect | Fix |
|---|---|---|---|
| 1 | 2 | `SqliteStore` was declared as `SqliteStore(EventBus, WorkQueue, RunStore)` in Task 2 but only implements `EventBus` until Task 4, so it could not be instantiated and Task 2's own tests could not pass. | Base classes widen task by task (`EventBus` → `+WorkQueue` → `+RunStore`). End state identical to the plan. |
| 2 | 9 | `dispatch_pending` drained the bus *before* checking the budget, so events held at the ceiling were consumed and destroyed — and the method's own `budget.exhausted` notice was swallowed on the next tick, which is what made the plan's own test fail. | Budget is checked before the drain; over-budget events stay on the bus. |

### Found by verification against the live SDK

| # | Task | Defect | Fix |
|---|---|---|---|
| 3 | 7 | **The write-scope guard did not work.** `can_use_tool` was wired as the enforcer while `Write`/`Edit` were also listed in `allowed_tools`. An `allowed_tools` entry that permits a whole tool auto-approves it *before* the callback is consulted, so the agent wrote straight through the guard — the SDK emits `CanUseToolShadowedWarning` saying exactly this. Task 7 Step 5 exists precisely to catch this, and did. | `build_options` excludes write tools from `allowed_tools`; both allow and deny paths re-verified live; regression test added. |

### Found by running it

| # | Defect | Fix |
|---|---|---|
| 4 | `agents up -v` — the form this plan and the README both document — was an argparse error. A flag declared on the top-level parser binds only *before* the subcommand. | `-v` declared on both, subcommand copy using `SUPPRESS` so it cannot clobber a leading `-v`. |
| 5 | **Nothing reclaimed an abandoned lease.** A supervisor killed mid-run left tasks in `leased` with no holder; `lease()` only picks `queued` rows. Because the dedupe index spans queued *and* leased rows, that unit of work then became permanently unqueueable — a silent deadlock. The plan's AWS mapping waved at this ("SQS visibility timeout") without noticing the local adapter implemented no equivalent. | `reclaim_expired_leases`, plus `Supervisor.recover()` at startup for orphaned worktrees and dangling run records. |
| 6 | **The budget guard stopped counting.** A per-run cap raises `ResultError` *instead of* yielding a `ResultMessage`, so the generic handler recorded `$0.00` for a run that spent its entire cap. The hourly ceiling sums those figures, so it undercounted exactly when spend was highest and could never trip. | Cost and turns read from `ResultError.data`; status `budget_exhausted`. |
| 7 | Capped runs were **retried** into the identical wall, spending the cap twice for nothing. | Terminal statuses (`budget_exhausted`, `max_turns`, `scope_rejected`) are dead-lettered, not requeued. |
| 8 | A capped run **discarded work the agent had already committed** — one `dep-updater` run correctly removed the unused `requests` pin and lost it to a cap that tripped afterwards. | An agent's own commits reach the gate however the run ended. An *uncommitted* worktree is still discarded. |
| 9 | `park_orphans()` scanned the filesystem and `rmtree`'d every child of the worktree root, including the merge gate's `integration/` directory — a *parent* of worktrees, not one itself. | Enumerates `git worktree list --porcelain`; also finds gate worktrees one level deeper than the old scan looked. |
| 10 | Per-run budgets were too tight: the one successful review used **$0.72 of a $0.75 cap** in 18 turns. Two sibling reviewers hit the wall. | Caps raised to $1.50 / $1.25 / $1.00 / $0.75. `HOURLY_BUDGET_USD` unchanged at $5.00, and now that accounting is honest it actually binds. |

### Observations that were not defects

- `CanUseToolShadowedWarning` for `Read`, `Grep`, `Glob`, `Bash` is expected and correct —
  those are read-only and deliberately pre-approved. Note that `Bash` *can* write via a shell
  redirect and the callback cannot see it; that is exactly why the merge gate re-checks scope
  against the branch's actual diff.
- The empirical simulator baseline is a **0.4056** player win rate over 5000 rounds with zero
  invariant violations, so the anomaly path depends on genuine drift rather than a seeded bug.
- Agent output quality was good where it was observed: the reviewer correctly identified an
  *empty* commit rather than manufacturing findings, and `dep-updater` grepped before acting
  and removed `requests` as unused rather than blindly bumping the pin.
