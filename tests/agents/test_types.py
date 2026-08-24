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
