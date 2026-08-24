from agents.worktree import WorktreeManager
from tests.agents.conftest import git


def make_manager(temp_repo, tmp_path):
    return WorktreeManager(repo_root=temp_repo, worktree_root=tmp_path / "wt", main_branch="main")


def test_create_makes_a_checkout_on_a_new_branch(temp_repo, tmp_path):
    wt = make_manager(temp_repo, tmp_path).create("reviewer")
    assert wt.path.is_dir()
    assert (wt.path / "README.md").exists()
    assert wt.branch.startswith("agent/reviewer-")
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == wt.branch


def test_two_worktrees_are_independent(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    a, b = mgr.create("reviewer"), mgr.create("test-author")
    assert a.path != b.path and a.branch != b.branch
    (a.path / "tests" / "new.py").write_text("x = 1\n")
    assert not (b.path / "tests" / "new.py").exists()


def test_park_removes_the_checkout_but_keeps_the_branch(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    (wt.path / "casino" / "hand.py").write_text("VALUE = 22\n")
    git(wt.path, "commit", "-qam", "change")
    branch = mgr.park(wt)
    assert not wt.path.exists()
    assert branch in git(temp_repo, "branch", "--list", branch)


def test_cleanup_removes_both_checkout_and_branch(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    mgr.cleanup(wt)
    assert not wt.path.exists()
    assert git(temp_repo, "branch", "--list", wt.branch) == ""


def test_cleanup_is_safe_to_call_twice(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    mgr.cleanup(wt)
    mgr.cleanup(wt)  # must not raise
