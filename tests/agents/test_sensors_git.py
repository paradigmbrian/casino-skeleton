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


def merge_branch(repo, branch, subject, path, content):
    """Land a branch the way MergeGate does: --no-ff, git-generated subject."""
    git(repo, "worktree", "add", "-q", "-b", branch, str(repo.parent / branch.replace("/", "-")), "main")
    wt = repo.parent / branch.replace("/", "-")
    target = wt / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", subject)
    git(repo, "worktree", "remove", "--force", str(wt))
    git(repo, "merge", "--no-ff", "--no-edit", branch)


def test_a_merge_of_the_reviewers_own_branch_is_skipped(temp_repo, sensor):
    """MergeGate lands work with `git merge --no-ff`, whose subject git writes as
    "Merge branch 'agent/reviewer-x'". That does not start with "review:", so a
    subject-prefix filter lets the reviewer's own output back through and the
    reviewer reviews its own review -- forever. Found by the reviewer itself."""
    sensor.poll()
    merge_branch(temp_repo, "agent/reviewer-x", "review: abc1234 looks fine",
                 "docs/reviews/abc1234.md", "# review\n")
    assert sensor.poll() == []


def test_a_merge_carrying_another_agents_work_still_wakes_the_reviewer(temp_repo, sensor):
    """The loop must close without closing the cascade: reviewing *other* agents'
    commits is the behaviour worth having."""
    sensor.poll()
    merge_branch(temp_repo, "agent/test-author-y", "test: cover the deck",
                 "tests/test_deck.py", "def test_deck():\n    assert True\n")
    events = sensor.poll()
    assert len(events) == 1, "a merged branch must wake the reviewer exactly once"
    assert events[0].payload["subject"] == "test: cover the deck"


def test_a_merged_branch_produces_one_event_not_one_per_commit(temp_repo, sensor):
    """Walking every commit emits the branch commit *and* the merge that lands
    it, so the same work is reviewed twice. Main's first-parent history is the
    record of what landed."""
    sensor.poll()
    git(temp_repo, "worktree", "add", "-q", "-b", "agent/multi", str(temp_repo.parent / "multi"), "main")
    wt = temp_repo.parent / "multi"
    for i in (1, 2, 3):
        (wt / "casino" / f"m{i}.py").write_text(f"X = {i}\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", f"feat: step {i}")
    git(temp_repo, "worktree", "remove", "--force", str(wt))
    git(temp_repo, "merge", "--no-ff", "--no-edit", "agent/multi")

    events = sensor.poll()
    assert len(events) == 1
    assert "step 1" in events[0].payload["subject"]
    assert "step 3" in events[0].payload["subject"]


def test_a_merge_mixing_agent_and_human_work_is_not_skipped(temp_repo, sensor):
    sensor.poll()
    git(temp_repo, "worktree", "add", "-q", "-b", "agent/mixed", str(temp_repo.parent / "mixed"), "main")
    wt = temp_repo.parent / "mixed"
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "r.md").write_text("# r\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "review: something")
    (wt / "casino" / "hand.py").write_text("VALUE = 99\n")
    git(wt, "commit", "-qam", "fix: a human change riding along")
    git(temp_repo, "worktree", "remove", "--force", str(wt))
    git(temp_repo, "merge", "--no-ff", "--no-edit", "agent/mixed")

    (event,) = sensor.poll()
    assert "fix: a human change riding along" in event.payload["subject"]


def test_the_hook_and_the_poller_share_one_predicate():
    """The reviewer's warning, made executable: if the fast path and the
    reconcile path disagree about what counts as self-authored, the hook stays
    silent while the poller still fires -- harder to diagnose than a consistent
    failure. The hook must call the shared predicate, not restate the rule."""
    from pathlib import Path

    hook = Path(__file__).resolve().parents[2] / "hooks" / "post-commit"
    text = hook.read_text()
    assert "from agents.sensors.git import" in text
    assert "is_self_authored" in text
    assert 'startswith("review:")' not in text, "hook re-implements the rule instead of sharing it"
    assert "--git-common-dir" in text, "hook must not fire inside a worker's worktree"
