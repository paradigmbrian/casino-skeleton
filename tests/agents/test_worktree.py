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


def test_park_orphans_keeps_branches_that_carry_work(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    (wt.path / "casino" / "hand.py").write_text("VALUE = 22\n")
    git(wt.path, "commit", "-qam", "review: a real change")

    assert mgr.park_orphans() == [wt.branch]
    assert not wt.path.exists()
    assert wt.branch in git(temp_repo, "branch", "--list", wt.branch)


def test_park_orphans_deletes_branches_with_nothing_on_them(temp_repo, tmp_path):
    """An interrupted run leaves a branch identical to main. There is nothing
    to inspect on it, and kept forever they pile up one per crash."""
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")

    assert mgr.park_orphans() == []
    assert not wt.path.exists()
    assert git(temp_repo, "branch", "--list", wt.branch) == ""


def test_park_orphans_is_a_noop_when_nothing_was_left_behind(temp_repo, tmp_path):
    assert make_manager(temp_repo, tmp_path).park_orphans() == []


def test_park_orphans_ignores_directories_that_are_not_worktrees(temp_repo, tmp_path):
    """The merge gate keeps its integration worktrees in a subdirectory of the
    worktree root. That directory is not itself a checkout, and code that runs
    rmtree must not delete things it does not recognise."""
    mgr = make_manager(temp_repo, tmp_path)
    stray = mgr.worktree_root / "integration"
    stray.mkdir(parents=True)
    (stray / "keep.txt").write_text("not a worktree\n")

    assert mgr.park_orphans() == []
    assert stray.is_dir()
    assert (stray / "keep.txt").exists()


def test_park_orphans_finds_worktrees_nested_below_the_root(temp_repo, tmp_path):
    """A crash mid-merge leaves a gate integration worktree one level deeper
    than a worker's, so a flat scan of the root would miss it."""
    mgr = make_manager(temp_repo, tmp_path)
    nested = mgr.worktree_root / "integration" / "integration-abc123"
    git(temp_repo, "worktree", "add", "-q", "--detach", str(nested), "main")
    assert nested.is_dir()

    mgr.park_orphans()
    assert not nested.exists()


def test_retire_keeps_a_branch_that_has_work(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")
    (wt.path / "casino" / "hand.py").write_text("VALUE = 22\n")
    git(wt.path, "commit", "-qam", "review: something")

    assert mgr.retire(wt) == wt.branch
    assert not wt.path.exists()
    assert wt.branch in git(temp_repo, "branch", "--list", wt.branch)


def test_retire_discards_a_branch_with_nothing_on_it(temp_repo, tmp_path):
    mgr = make_manager(temp_repo, tmp_path)
    wt = mgr.create("reviewer")

    assert mgr.retire(wt) is None
    assert git(temp_repo, "branch", "--list", wt.branch) == ""
