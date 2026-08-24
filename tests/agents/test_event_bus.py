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
