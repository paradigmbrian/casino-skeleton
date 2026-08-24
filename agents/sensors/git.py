from __future__ import annotations

import logging
from pathlib import Path

from agents.types import Event
from agents.worktree import run_git

LOG = logging.getLogger("agents.sensors.git")

META_KEY = "git.last_sha"
SELF_AUTHORED_PREFIXES = ("review:",)  # the reviewer must not review itself


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

        out = run_git(self.repo_root, "log", "--reverse", "--pretty=%H%x1f%s",
                      f"{last}..{head}").stdout
        self.store.set_meta(META_KEY, head)

        events: list[Event] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            sha, _, subject = line.partition("\x1f")
            if subject.startswith(SELF_AUTHORED_PREFIXES):
                LOG.debug("skipping self-authored commit %s", sha[:8])
                continue
            events.append(Event(type="commit.pushed",
                                payload={"sha": sha, "short_sha": sha[:8], "subject": subject},
                                source="git_sensor"))
        return events
