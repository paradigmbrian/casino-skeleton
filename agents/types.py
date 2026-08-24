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
