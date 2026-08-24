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
