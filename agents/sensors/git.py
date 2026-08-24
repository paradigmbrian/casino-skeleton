from __future__ import annotations

import logging
from pathlib import Path

from agents.types import Event
from agents.worktree import run_git

LOG = logging.getLogger("agents.sensors.git")

META_KEY = "git.last_sha"
SELF_AUTHORED_PREFIXES = ("review:",)  # the reviewer must not review itself


def introduced_subjects(repo_root: Path, sha: str) -> list[str]:
    """The subjects a commit actually *introduces*.

    For an ordinary commit that is its own subject. For a merge it is the
    subjects of the commits coming in from the merged side, because the merge's
    own subject is written by git ("Merge branch 'agent/reviewer-x'") and says
    nothing about who authored the work."""
    header = run_git(repo_root, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    is_merge = len(header) > 2  # sha + two or more parents
    if is_merge:
        out = run_git(repo_root, "log", "--no-merges", "--pretty=%s",
                      f"{sha}^1..{sha}").stdout
        return [line for line in out.splitlines() if line.strip()]
    return [run_git(repo_root, "log", "-1", "--pretty=%s", sha).stdout.strip()]


def describe(repo_root: Path, sha: str) -> str:
    """What a commit brings in, phrased for a worker's task brief. A merge's own
    subject ("Merge branch 'agent/test-author-y'") says nothing useful."""
    return "; ".join(introduced_subjects(repo_root, sha))


def is_self_authored(repo_root: Path, sha: str) -> bool:
    """True when everything a commit brings in was written by the reviewer.

    Matching the merge commit's own subject is not enough: the merge gate lands
    every branch with `git merge --no-ff`, so the reviewer's work arrives under a
    git-generated subject and slips past a prefix test. That produced a genuine
    unbounded loop -- reviewer reviews its own review, forever, each merge
    carrying a fresh SHA so dedupe never catches it."""
    subjects = introduced_subjects(repo_root, sha)
    return bool(subjects) and all(s.startswith(SELF_AUTHORED_PREFIXES) for s in subjects)


class GitSensor:
    """Push plus reconcile. The post-commit hook publishes immediately; this
    poller catches anything committed while the layer was down."""

    def __init__(self, repo_root: Path, store, main_branch: str = "main"):
        self.repo_root = Path(repo_root)
        self.store = store
        self.main_branch = main_branch

    def poll(self) -> list[Event]:
        head = run_git(self.repo_root, "rev-parse", self.main_branch).stdout.strip()
        last = self.store.get_meta(META_KEY)

        if last is None:
            self.store.set_meta(META_KEY, head)
            LOG.info("git baseline set to %s", head[:8])
            return []
        if last == head:
            return []

        # --first-parent walks what actually landed on main. Without it a merged
        # branch is announced twice -- once for its own commits, once for the
        # merge that lands them -- and the same work is reviewed twice.
        out = run_git(self.repo_root, "log", "--reverse", "--first-parent",
                      "--pretty=%H", f"{last}..{head}").stdout
        self.store.set_meta(META_KEY, head)

        events: list[Event] = []
        for sha in (line.strip() for line in out.splitlines()):
            if not sha:
                continue
            if is_self_authored(self.repo_root, sha):
                LOG.debug("skipping self-authored commit %s", sha[:8])
                continue
            events.append(Event(
                type="commit.pushed",
                payload={"sha": sha, "short_sha": sha[:8],
                         "subject": describe(self.repo_root, sha)},
                source="git_sensor"))
        return events
