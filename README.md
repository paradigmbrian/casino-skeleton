# Casino Skeleton — with an autonomous maintenance layer

A blackjack simulator (`casino/`) plus an agent layer (`agents/`) that maintains it with no
human in the loop. Install with `uv pip install --python .venv/bin/python -r
agents/requirements.txt`, link the hook with `ln -sf ../../hooks/post-commit
.git/hooks/post-commit`, export `ANTHROPIC_API_KEY`, then run `.venv/bin/python -m agents.cli
up` — and `status`, `events`, or `stop` in another terminal ([full instructions](docs/running.md)).
Sensors publish typed events to a SQLite bus and a deterministic orchestrator routes each to
one of four workers, every one of them fired by something other than a person: **`reviewer`**
wakes on `commit.pushed` from the post-commit hook or the git poller, reads the diff and its
surroundings, writes a review under `docs/reviews/`, and fixes at most one high-confidence
finding in `casino/`; **`test-author`** wakes on `coverage.gap` from a coverage sensor (or a
`test.failed` from the merge gate) and writes real tests under `tests/`; **`dep-updater`**
wakes on `deps.stale` when a timer sensor finds a pinned package behind its latest PyPI
release, and bumps or removes it; **`anomaly-investigator`** wakes on `outcome.anomaly` when a
z-test over `outcomes.jsonl` detects drift from the recorded baseline, and writes a diagnosis
under `docs/investigations/`. Each runs `claude-agent-sdk` in its own git worktree under a
write scope it cannot escape — the scopes are disjoint, so no agent can silence another's
alarms — and nothing reaches `main` except through a gate that checks scope, re-runs the full
suite, then fast-forwards ([architecture](docs/architecture.md)).

**AI tools:** Claude Code built the layer, directed and reviewed by me throughout — the
[design spec](docs/superpowers/specs/2026-08-23-autonomous-agent-layer-design.md) and
[implementation plan](docs/superpowers/plans/2026-08-23-autonomous-agent-layer.md) were
written first and are in the repo, and the code was implemented test-first against that plan;
`claude-agent-sdk` with `claude-opus-5` is the workers' own runtime, while the control plane
makes zero model calls. **What did not go as planned:** the write-scope guard did not work on
the first try — listing a tool in `allowed_tools` auto-approves it *before* the permission
callback runs, so the agent wrote straight through it, caught only because the plan forced a
live verification step; the hourly budget guard silently recorded `$0.00` for runs that hit
their per-run cap, disabling the one rail that bounds spend, and along the way threw away work
agents had already committed; a killed supervisor deadlocked its own queue, because nothing
reclaimed an abandoned lease and the dedupe index covered leased rows; and the reviewer itself
found and documented an unbounded review-of-a-review loop in its own trigger, which is now
fixed ([full postmortem](docs/postmortem.md)). **With more time**, in priority order: fix
worker starvation — a single FIFO queue and two slots let reviewers monopolise both, which is
the only reason `dep-updater`'s finished work never reached the gate — then rebalance the
economics so the layer stops spending most of its budget reviewing itself, and prove the
`anomaly-investigator`, whose trigger has never legitimately fired ([status and next
steps](docs/status.md)).

---

**Docs:** [Running it](docs/running.md) · [Architecture](docs/architecture.md) ·
[Status & what to do next](docs/status.md) · [Postmortem](docs/postmortem.md) ·
[AWS mapping](docs/aws-mapping.md) · [Reviews the agents wrote](docs/reviews/)
