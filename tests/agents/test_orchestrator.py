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
