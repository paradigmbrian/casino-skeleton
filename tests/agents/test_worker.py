import asyncio

import pytest

from agents.worker import WorkerSpec, ensure_committed, make_scope_guard
from tests.agents.conftest import git


def decide(guard, tool_name, tool_input):
    class Ctx:
        suggestions = []
    return asyncio.run(guard(tool_name, tool_input, Ctx()))


@pytest.fixture
def guard(tmp_path):
    (tmp_path / "tests").mkdir()
    return make_scope_guard(("tests/",), tmp_path)


def test_write_inside_scope_is_allowed(guard, tmp_path):
    result = decide(guard, "Write", {"file_path": str(tmp_path / "tests" / "test_new.py")})
    assert result.behavior == "allow"


def test_write_outside_scope_is_denied_with_a_useful_message(guard, tmp_path):
    result = decide(guard, "Write", {"file_path": str(tmp_path / "casino" / "table.py")})
    assert result.behavior == "deny"
    assert "casino/table.py" in result.message
    assert "write scope" in result.message


def test_edit_is_gated_the_same_way_as_write(guard, tmp_path):
    assert decide(guard, "Edit", {"file_path": str(tmp_path / "casino" / "hand.py")}).behavior == "deny"
    assert decide(guard, "Edit", {"file_path": str(tmp_path / "tests" / "t.py")}).behavior == "allow"


def test_writes_escaping_the_worktree_are_denied(guard, tmp_path):
    assert decide(guard, "Write", {"file_path": "/etc/passwd"}).behavior == "deny"
    assert decide(guard, "Write", {"file_path": str(tmp_path.parent / "elsewhere.py")}).behavior == "deny"


def test_read_only_tools_are_always_allowed(guard):
    for tool in ("Read", "Grep", "Glob", "Bash"):
        assert decide(guard, tool, {"command": "pytest -q"}).behavior == "allow"


def test_a_write_with_no_path_argument_is_denied(guard):
    assert decide(guard, "Write", {}).behavior == "deny"


def test_ensure_committed_commits_leftover_changes(temp_repo):
    (temp_repo / "tests" / "leftover.py").write_text("x = 1\n")
    assert ensure_committed(temp_repo, "chore: leftovers") is True
    assert git(temp_repo, "status", "--porcelain") == ""
    assert "leftovers" in git(temp_repo, "log", "-1", "--pretty=%s")


def test_ensure_committed_is_a_noop_on_a_clean_tree(temp_repo):
    before = git(temp_repo, "rev-parse", "HEAD")
    assert ensure_committed(temp_repo, "chore: nothing") is False
    assert git(temp_repo, "rev-parse", "HEAD") == before


def test_worker_spec_defaults_are_conservative():
    spec = WorkerSpec(name="w", triggers=("x",), system_prompt="p",
                      allowed_tools=("Read",), write_scope=("tests/",))
    assert spec.max_turns == 25
    assert spec.timeout_s == 300
    assert spec.max_cost_usd == 0.50


def test_write_tools_are_left_out_of_allowed_tools_so_the_guard_is_consulted(tmp_path):
    """An allowed_tools entry that allows a whole tool auto-approves it before
    can_use_tool runs, which would shadow the scope guard. Verified live against
    claude-agent-sdk 0.2.144, which warns about exactly this."""
    from agents.worker import build_options

    spec = WorkerSpec(name="w", triggers=("x",), system_prompt="p",
                      allowed_tools=("Read", "Grep", "Bash", "Write", "Edit"),
                      write_scope=("tests/",))
    options = build_options(spec, tmp_path)
    assert "Write" not in options.allowed_tools
    assert "Edit" not in options.allowed_tools
    assert options.allowed_tools == ["Read", "Grep", "Bash"]
    assert options.can_use_tool is not None
    assert options.permission_mode == "default"
