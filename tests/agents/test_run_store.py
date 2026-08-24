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
