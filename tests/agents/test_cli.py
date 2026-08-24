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
