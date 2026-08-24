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
