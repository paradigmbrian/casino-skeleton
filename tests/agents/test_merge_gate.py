import pytest

from agents.merge_gate import MergeGate
from tests.agents.conftest import git

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_bad():\n    assert False\n"


def branch_with(repo, branch, rel_path, content, message="agent change"):
    """Commit `content` at `rel_path` on a new branch off main, without
    disturbing the primary checkout."""
    git(repo, "worktree", "add", "-q", "-b", branch, str(repo.parent / branch.replace("/", "-")), "main")
    wt = repo.parent / branch.replace("/", "-")
    target = wt / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", message)
    git(repo, "worktree", "remove", "--force", str(wt))
    return branch


@pytest.fixture
def gate(temp_repo, tmp_path):
    # A test command we control, so gate tests never depend on the real suite.
    return MergeGate(repo_root=temp_repo, worktree_root=tmp_path / "integration",
                     main_branch="main",
                     test_cmd=["python", "-c",
                               "import pathlib,sys; "
                               "sys.exit(1 if 'assert False' in "
                               "''.join(p.read_text() for p in pathlib.Path('tests').rglob('*.py')) "
                               "else 0)"])


def test_in_scope_passing_change_is_merged_into_main(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-a", "tests/test_new.py", PASSING)
    result = gate.submit("agent/tests-a", write_scope=("tests/",))
    assert result.status == "merged"
    assert result.changed_files == ("tests/test_new.py",)
    assert result.sha
    assert (temp_repo / "tests" / "test_new.py").exists()


def test_out_of_scope_change_is_rejected_before_tests_run(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-b", "casino/hand.py", "VALUE = 99\n")
    result = gate.submit("agent/tests-b", write_scope=("tests/",))
    assert result.status == "scope_rejected"
    assert "casino/hand.py" in result.detail
    assert (temp_repo / "casino" / "hand.py").read_text() == "VALUE = 21\n"  # main untouched


def test_failing_tests_block_the_merge(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-c", "tests/test_broken.py", FAILING)
    result = gate.submit("agent/tests-c", write_scope=("tests/",))
    assert result.status == "tests_failed"
    assert not (temp_repo / "tests" / "test_broken.py").exists()


def test_a_branch_with_no_changes_reports_empty(temp_repo, gate):
    git(temp_repo, "branch", "agent/tests-d", "main")
    assert gate.submit("agent/tests-d", write_scope=("tests/",)).status == "empty"


def test_a_dirty_primary_checkout_blocks_merging(temp_repo, gate):
    branch_with(temp_repo, "agent/tests-e", "tests/test_new.py", PASSING)
    (temp_repo / "README.md").write_text("# locally edited\n")
    assert gate.submit("agent/tests-e", write_scope=("tests/",)).status == "dirty_main"


def test_the_integration_worktree_is_always_cleaned_up(temp_repo, gate, tmp_path):
    branch_with(temp_repo, "agent/tests-f", "tests/test_new.py", PASSING)
    gate.submit("agent/tests-f", write_scope=("tests/",))
    leftovers = list((tmp_path / "integration").glob("*")) if (tmp_path / "integration").exists() else []
    assert leftovers == []
