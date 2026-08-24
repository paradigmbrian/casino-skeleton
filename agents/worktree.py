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

    def commits_ahead(self, branch: str) -> int:
        proc = run_git(self.repo_root, "rev-list", "--count",
                       f"{self.main_branch}..{branch}", check=False)
        return int(proc.stdout.strip() or 0) if proc.returncode == 0 else 0

    def _registered_worktrees(self) -> list[Worktree]:
        """Ask git what the worktrees actually are. Scanning the filesystem
        instead would treat any stray directory under the root as a checkout --
        including the merge gate's integration directory, which is a parent of
        worktrees rather than one itself."""
        out = run_git(self.repo_root, "worktree", "list", "--porcelain", check=False).stdout
        found: list[Worktree] = []
        path: Path | None = None
        branch = ""
        for line in out.splitlines() + [""]:
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
                branch = ""
            elif line.startswith("branch "):
                branch = line[len("branch "):].removeprefix("refs/heads/")
            elif not line.strip() and path is not None:
                if path != self.repo_root.resolve() and path != self.repo_root:
                    found.append(Worktree(path=path, branch=branch))
                path, branch = None, ""
        return found

    def retire(self, wt: Worktree) -> str | None:
        """Finish with a worktree whose run did not merge. A branch carrying
        commits is parked so a human can read what the agent tried; a branch
        identical to main has nothing on it to read, and keeping one per failed
        run just accumulates stubs. Returns the branch kept, or None."""
        if wt.branch and self.commits_ahead(wt.branch) > 0:
            return self.park(wt)
        self.cleanup(wt)
        return None

    def park_orphans(self) -> list[str]:
        """Clear every checkout under the worktree root. Only ever called at
        startup, when by definition no run is in flight, so anything still there
        was abandoned.

        A branch carrying commits is parked -- the checkout goes, the branch
        stays inspectable. A branch identical to main has nothing to inspect and
        is deleted, otherwise every interrupted run leaves a stub behind forever.
        Returns the branches actually kept."""
        parked: list[str] = []
        root = self.worktree_root.resolve()
        for wt in self._registered_worktrees():
            try:
                wt.path.resolve().relative_to(root)
            except ValueError:
                continue  # not ours; leave other people's worktrees alone
            kept = self.retire(wt)
            if kept:
                parked.append(kept)
        return parked

    def cleanup(self, wt: Worktree) -> None:
        self.park(wt)
        run_git(self.repo_root, "branch", "-D", wt.branch, check=False)
