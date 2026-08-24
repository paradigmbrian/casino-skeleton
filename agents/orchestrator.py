from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from agents.config import HOURLY_BUDGET_USD, MAX_CASCADE_DEPTH, ROUTES
from agents.logfmt import event_kwargs
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
        # Budget is checked *before* draining. Draining consumes, so checking
        # afterwards would destroy the held work -- and would swallow this
        # method's own budget notice on the following tick. Over budget, events
        # stay on the bus and are dispatched once the hour rolls forward.
        if self.budget_remaining() <= 0:
            if not self._budget_notice_sent:
                self.store.publish(Event(
                    type="budget.exhausted",
                    payload={"hourly_budget_usd": self.hourly_budget_usd,
                             "spent_usd": round(self._spent_this_hour(), 4)},
                    source="orchestrator",
                ))
                self._budget_notice_sent = True
                LOG.warning("", extra=event_kwargs(
                    "budget", "orchestrator",
                    f"hourly ceiling reached (${self._spent_this_hour():.2f} of "
                    f"${self.hourly_budget_usd:.2f}); holding pending events"))
            return []
        self._budget_notice_sent = False

        events = self.store.drain()
        if not events:
            return []

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
                LOG.info("", extra=event_kwargs(
                    "dispatch", event.type, f"→ {worker}   depth {event.depth}"))
                dispatched.append(task)
            else:
                LOG.debug("deduped %s -> %s", event.type, worker)
        return dispatched
