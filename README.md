# Casino Skeleton — with an autonomous maintenance layer

A blackjack simulator (`casino/`) plus an agent layer (`agents/`) that maintains it with no
human in the loop. Sensors publish typed events to a SQLite bus; a deterministic orchestrator
routes each to one of four workers, every one fired by something other than a person. Each
worker runs `claude-agent-sdk` inside its own git worktree under a write scope it cannot
escape — the scopes are disjoint, so no agent can silence another's alarms — and nothing
reaches `main` except through a gate that checks scope, re-runs the full test suite, then
fast-forwards.

## Running it

```bash
uv pip install --python .venv/bin/python -r agents/requirements.txt
ln -sf ../../hooks/post-commit .git/hooks/post-commit   # .git/hooks is untracked, so this is a setup step
export ANTHROPIC_API_KEY=...

.venv/bin/python -m agents.cli up                        # start the layer
```

In another terminal: `status` (queue depth, recent runs, spend), `events` (`--follow` to
tail), `stop` (drain and exit). Full detail in [docs/running.md](docs/running.md).

## The agents and their triggers

| Agent | Trigger | Fired by | Writes to | What it does |
|---|---|---|---|---|
| `reviewer` | `commit.pushed` | post-commit hook, git poller | `casino/`, `docs/reviews/` | Reads the diff and its surroundings, writes a review, fixes at most one high-confidence finding |
| `test-author` | `coverage.gap`, `test.failed` | coverage sensor, merge gate | `tests/` | Writes real tests for under-covered modules. Cannot touch `casino/`, so it can never fix a failure by editing the code under test |
| `dep-updater` | `deps.stale` | timer sensor, via the PyPI API | `requirements.txt`, `docs/dependencies.md` | Bumps or removes a stale pin, verifies green, records the reasoning |
| `anomaly-investigator` | `outcome.anomaly` | z-test over `outcomes.jsonl` | `docs/investigations/` | Diagnoses drift or impossible outcomes. Writes no code — it hands findings to the other two |

No agent is triggered by a human typing anything. The only manual action is starting the
supervisor. See [docs/architecture.md](docs/architecture.md) for how the three planes fit
together and why the write scopes are disjoint.

## AI tools used

Claude Code built this layer, directed and reviewed by me throughout. The
[design spec](docs/superpowers/specs/2026-08-23-autonomous-agent-layer-design.md) and
[implementation plan](docs/superpowers/plans/2026-08-23-autonomous-agent-layer.md) were
written first and are in the repo; the code was implemented test-first against that plan.
`claude-agent-sdk` with `claude-opus-5` is the workers' own runtime. The control plane makes
zero model calls, by design.

## What did not go as planned

- **The write-scope guard did not work.** Listing a tool in `allowed_tools` auto-approves it
  *before* the permission callback runs, so the agent wrote straight through it. Caught only
  because the plan forced a live verification step rather than trusting the unit tests.
- **The budget guard silently stopped counting.** A run that hit its per-run cap was recorded
  as costing `$0.00`, so the hourly ceiling — computed by summing those figures — undercounted
  exactly when spend was highest and could never trip. It also discarded work agents had
  already finished and committed.
- **A killed supervisor deadlocked its own queue.** Nothing reclaimed an abandoned lease, and
  the dedupe index covers leased rows, so that unit of work became permanently unqueueable.
- **The reviewer found an unbounded loop in its own trigger.** Merge commits bypassed the
  self-review filter, so the reviewer reviewed its own reviews, forever. Its
  [review](docs/reviews/7ad57a1.md) diagnosed it with a replay over real history.

Full detail, including the four safety rails that did not hold as designed, in
[docs/postmortem.md](docs/postmortem.md).

## What I would improve with more time

1. **Fix worker starvation.** A single FIFO queue and two slots let reviewers monopolise both.
   That is the only reason `dep-updater`'s finished work never reached the gate.
2. **Rebalance the economics.** The layer spends most of its budget reviewing itself; reviews
   trigger reviews, and reviewing the agent layer is far more expensive than reviewing `casino/`.
3. **Prove the `anomaly-investigator`.** Its trigger has never legitimately fired — the
   simulator's baseline is clean, so there is genuinely nothing anomalous to find.

What has actually landed, with evidence, and the full prioritised list:
[docs/status.md](docs/status.md).

---

**Docs:** [Running it](docs/running.md) · [Architecture](docs/architecture.md) ·
[Status & next steps](docs/status.md) · [Postmortem](docs/postmortem.md) ·
[AWS mapping](docs/aws-mapping.md) · [Reviews the agents wrote](docs/reviews/)
