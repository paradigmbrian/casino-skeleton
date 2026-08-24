import subprocess

import pytest


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def temp_repo(tmp_path):
    """A throwaway git repo with one commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "agent@test.local")
    git(repo, "config", "user.name", "Agent Test")
    (repo / "README.md").write_text("# temp\n")
    (repo / "casino").mkdir()
    (repo / "casino" / "hand.py").write_text("VALUE = 21\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo
