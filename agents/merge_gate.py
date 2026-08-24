from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from agents.scope import out_of_scope
from agents.worktree import run_git


@dataclass(frozen=True)
class GateResult:
    status: str  # merged|scope_rejected|tests_failed|merge_conflict|empty|dirty_main
    changed_files: tuple[str, ...] = ()
    sha: str | None = None
    detail: str = ""


class MergeGate:
    """Serialized single writer to main. Verifies scope, then verifies tests,
    then fast-forwards. Never resolves conflicts -- disjoint write scopes mean
    a conflict is a bug worth surfacing, not something to paper over."""

    def __init__(self, repo_root: Path, worktree_root: Path, main_branch: str = "main",
                 test_cmd: list[str] | None = None):
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root)
        self.main_branch = main_branch
        self.test_cmd = test_cmd or [sys.executable, "-m", "pytest", "-q"]
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    def _changed_files(self, branch: str) -> tuple[str, ...]:
        out = run_git(self.repo_root, "diff", "--name-only",
                      f"{self.main_branch}...{branch}").stdout
        return tuple(line for line in out.splitlines() if line.strip())

    def _primary_is_clean(self) -> bool:
        out = run_git(self.repo_root, "status", "--porcelain", "--untracked-files=no").stdout
        return out.strip() == ""

    def submit(self, branch: str, write_scope: tuple[str, ...]) -> GateResult:
        changed = self._changed_files(branch)
        if not changed:
            return GateResult("empty", detail=f"{branch} has no changes against {self.main_branch}")

        offenders = out_of_scope(changed, write_scope)
        if offenders:
            return GateResult("scope_rejected", changed,
                              detail=f"outside write scope {write_scope}: {offenders}")

        if not self._primary_is_clean():
            return GateResult("dirty_main", changed,
                              detail="primary checkout has uncommitted changes; refusing to merge")

        integration = self.worktree_root / f"integration-{uuid.uuid4().hex[:8]}"
        try:
            run_git(self.repo_root, "worktree", "add", "--detach", str(integration), self.main_branch)
            merge = run_git(integration, "merge", "--no-edit", branch, check=False)
            if merge.returncode != 0:
                return GateResult("merge_conflict", changed, detail=merge.stderr.strip()[:800])

            proc = subprocess.run(self.test_cmd, cwd=integration, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = (proc.stdout + proc.stderr).strip()[-1500:]
                return GateResult("tests_failed", changed, detail=tail)
        finally:
            run_git(self.repo_root, "worktree", "remove", "--force", str(integration), check=False)
            if integration.exists():
                shutil.rmtree(integration, ignore_errors=True)
            run_git(self.repo_root, "worktree", "prune", check=False)

        run_git(self.repo_root, "merge", "--no-ff", "--no-edit", branch)
        sha = run_git(self.repo_root, "rev-parse", "HEAD").stdout.strip()
        return GateResult("merged", changed, sha=sha, detail=f"merged {branch}")
