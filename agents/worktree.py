from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


class WorktreeManager:
    """One isolated checkout per worker run. Workers never see the primary
    checkout, so two agents cannot collide on the working tree."""

    def __init__(self, repo_root: Path, worktree_root: Path, main_branch: str = "main"):
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root)
        self.main_branch = main_branch
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    def create(self, name_hint: str) -> Worktree:
        slug = f"{name_hint}-{uuid.uuid4().hex[:8]}"
        branch = f"agent/{slug}"
        path = self.worktree_root / slug
        run_git(self.repo_root, "worktree", "add", "-b", branch, str(path), self.main_branch)
        return Worktree(path=path, branch=branch)

    def park(self, wt: Worktree) -> str:
        """Drop the checkout, keep the branch so a human can inspect the work."""
        run_git(self.repo_root, "worktree", "remove", "--force", str(wt.path), check=False)
        if wt.path.exists():
            shutil.rmtree(wt.path, ignore_errors=True)
        run_git(self.repo_root, "worktree", "prune", check=False)
        return wt.branch

    def _commits_ahead(self, branch: str) -> int:
        proc = run_git(self.repo_root, "rev-list", "--count",
                       f"{self.main_branch}..{branch}", check=False)
        return int(proc.stdout.strip() or 0) if proc.returncode == 0 else 0

    def park_orphans(self) -> list[str]:
        """Clear every checkout under the worktree root. Only ever called at
        startup, when by definition no run is in flight, so anything still there
        was abandoned.

        A branch carrying commits is parked -- the checkout goes, the branch
        stays inspectable. A branch identical to main has nothing to inspect and
        is deleted, otherwise every interrupted run leaves a stub behind forever.
        Returns the branches actually kept."""
        parked: list[str] = []
        if not self.worktree_root.exists():
            return parked
        for path in sorted(self.worktree_root.iterdir()):
            if not path.is_dir():
                continue
            proc = run_git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
            branch = proc.stdout.strip() if proc.returncode == 0 else ""
            wt = Worktree(path=path, branch=branch)
            if branch and self._commits_ahead(branch) == 0:
                self.cleanup(wt)
                continue
            self.park(wt)
            parked.append(branch or str(path))
        return parked

    def cleanup(self, wt: Worktree) -> None:
        self.park(wt)
        run_git(self.repo_root, "branch", "-D", wt.branch, check=False)
