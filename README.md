# Casino Skeleton — with an autonomous maintenance layer

A small blackjack simulator (`casino/`) plus an agent layer (`agents/`) that maintains it
without a human in the loop. Four LLM workers are woken by non-human triggers — a commit
landing, a coverage gap, a stale dependency pin, a statistical anomaly in simulation
outcomes — each works inside its own git worktree under a write scope it cannot escape, and
nothing reaches `main` except through a gate that re-runs the full test suite first.

The agents' commits are in this repository's history, above the template's `Initial commit`.

## Running it

```bash
# 1. Dependencies (the agent layer's, kept separate from the project's own requirements.txt)
uv pip install --python .venv/bin/python -r agents/requirements.txt

# 2. Install the post-commit hook. .git/hooks/ is not tracked by git, so this is a setup
#    step on every fresh clone, not something a commit can carry.
ln -sf ../../hooks/post-commit .git/hooks/post-commit

# 3. Credentials
export ANTHROPIC_API_KEY=...

# 4. Run it
.venv/bin/python -m agents.cli up -v
```

While it runs, in another terminal:

```bash
.venv/bin/python -m agents.cli status          # queue depth, recent runs, spend this hour
.venv/bin/python -m agents.cli events          # the event log; --follow to tail it
.venv/bin/python -m agents.cli stop            # drain and exit (Ctrl-C works too)
```

`stop` writes `agents/state/STOP`, which every loop checks. In-flight work finishes; nothing
new is dispatched.

## The agents and their triggers

| Worker | Trigger event | Emitted by | Write scope | What it does |
|---|---|---|---|---|
| `reviewer` | `commit.pushed`, `review.fix_requested` | post-commit hook / `git_sensor` | `casino/`, `docs/reviews/` | Reads the diff and its surroundings, writes a review per commit, and fixes at most one high-confidence, low-risk finding. |
| `test-author` | `coverage.gap`, `test.failed`, `regression.needed` | `coverage_sensor`, merge gate, investigator handoff | `tests/` | Writes real tests for under-covered modules. Cannot touch `casino/`, so it can never make a failure go away by editing the code under test. |
| `dep-updater` | `deps.stale` | `timer_sensor` (PyPI JSON API) | `requirements.txt`, `docs/dependencies.md` | Bumps or removes a pin, verifies green, and records the reasoning. |
| `anomaly-investigator` | `outcome.anomaly`, `outcome.invariant_violation` | `anomaly_sensor` | `docs/investigations/` | Diagnoses drift or impossible outcomes. Writes no code — it hands findings to the other two. |

No worker is triggered by a human typing anything. The only human action is starting the
supervisor.

## How it works

Three planes, each deliberately dumber than the one above it.

```
  SENSORS (no LLM)                CONTROL PLANE (no LLM)              WORKERS (LLM)
  ────────────────                ──────────────────────              ─────────────
  git_sensor       ─┐                                            ┌─► reviewer
  coverage_sensor  ─┤   Event    ┌──────────────┐    Task        ├─► test-author
  timer_sensor     ─┼──────────► │  event bus   │ ──────────────►├─► dep-updater
  anomaly_sensor   ─┤            │  orchestrator│                └─► anomaly-investigator
  post-commit hook ─┘            │  work queue  │                        │
                                 └──────────────┘                        │ branch
                                        ▲                                ▼
                                        │  new events            ┌───────────────┐
                                        └────────────────────────│   merge gate  │──► main
                                           (cascade)             └───────────────┘
```

**Sensors** are pure Python. They observe and publish typed events; they make no model calls
and hold no opinions. The PyPI lookup lives in `timer_sensor` rather than in `dep-updater`
so that the *trigger* stays deterministic even when a model call flakes.

**The orchestrator** is a dictionary. `config.ROUTES` maps event type to worker name, and
that is the entire dispatch logic — given the same events it produces the same dispatches,
every time. It also enforces the three rails: a cascade depth cap (an agent's work can
trigger work, but only three levels deep), an hourly spend ceiling, and dedupe via a partial
unique index on the work's identity hash, so the same gap observed twice is queued once.

**Workers** run `claude-agent-sdk` with `claude-opus-5` inside a git worktree created for
that run. Two enforcement layers protect the write scope:

1. *Prevention* — a `can_use_tool` callback checks every `Write`/`Edit` against the worker's
   scope before it reaches the filesystem. A denied write returns a message explaining why,
   so the agent reports the problem instead of thrashing.
2. *Detection* — the merge gate re-checks the same predicate against the branch's actual
   diff. This is the backstop for anything that arrives by a route the callback does not see,
   such as a shell redirect through `Bash`.

**The merge gate** is the only writer to `main`, serialized behind a lock. Its order matters:
scope is checked *before* tests, because an out-of-scope diff is a policy violation whether
or not it happens to pass. Then it merges into a throwaway integration worktree, runs the
full suite there, and only fast-forwards `main` if everything is green. A failing suite
publishes `test.failed`, which routes back to `test-author` — the loop closes itself.

### Why the write scopes are disjoint

`reviewer` owns `casino/`. `test-author` owns `tests/`. They do not overlap, and a test in
`tests/agents/test_specs.py` asserts pairwise disjointness so the invariant cannot rot.

This is not tidiness. It is what makes the layer safe to leave running. If one agent owned
both, the cheapest way to resolve a failing test would be to edit the test — an agent that
can silence its own alarms is worse than no agent. Splitting the scopes makes that move
structurally impossible: when `test-author` writes a failing test, its only options are to
fix the test or to report a suspected bug. The `anomaly-investigator` takes the same idea
further and owns no code at all, which forces its output to be good enough for another agent
to act on.

The same reasoning explains the merge gate's existence. Four agents committing concurrently
to one branch is a conflict-resolution problem; four agents committing to their own worktrees
and queueing at a serialized gate is not.

## Transparency

Every run is recorded twice, before and after:

- `agents/state/runs.jsonl` — append-only ledger, one JSON object per state change: worker,
  event type, branch, status, cost, turn count, files changed, and the agent's own summary.
- `agents/state/agents.db` — the same records in SQLite, plus the full event log (including
  which events were consumed and when) and the task queue with attempt counts.

Both are gitignored, since they are runtime state rather than source. After a run:

```bash
.venv/bin/python -m agents.cli status                     # the summary view
.venv/bin/python -m agents.cli events --limit 100         # what fired, in order
tail -5 agents/state/runs.jsonl | python -m json.tool     # the raw record
sqlite3 agents/state/agents.db 'SELECT worker, status, cost_usd FROM runs'
git log --oneline                                          # what actually landed
```

A rejected branch is *parked*, not deleted: the worktree is removed but the branch survives,
so `git log agent/<worker>-<id>` shows exactly what an agent tried to do and the ledger says
why it was refused.

## AI tools used

- **Claude Code** built this layer, directed and reviewed by a human throughout. The design
  (`docs/superpowers/specs/`) and implementation plan (`docs/superpowers/plans/`) were
  written first and are in the repository; the code was implemented test-first against that
  plan. Two plan defects found during implementation are noted below.
- **`claude-agent-sdk` 0.2.144** with **`claude-opus-5`** (`effort="high"`, adaptive
  thinking) is the workers' runtime. `setting_sources=None` on every worker, so agents do
  not inherit this repo's `CLAUDE.md` or the operator's global settings — a worker's
  behaviour is fully described by its `WorkerSpec`.
- The control plane makes **zero** model calls, by design.

## What did not go as planned, and what I would improve

**The permission guard was silently bypassed on the first try.** The plan wired `can_use_tool`
as the write-scope enforcer while also listing `Write` and `Edit` in `allowed_tools`. Verified
against the live SDK, the agent wrote a file straight through the guard: an `allowed_tools`
entry that permits a whole tool auto-approves it *before* the callback is consulted, and the
SDK warns about exactly this. The fix is in `agents/worker.py:build_options` — write tools are
excluded from `allowed_tools` so every write falls through to the guard, with both the allow
and deny paths re-verified live and a regression test pinning the behaviour. Had this shipped
unverified, the layer would have looked safe while relying entirely on the merge gate to catch
violations after the fact.

**The orchestrator destroyed work when over budget.** As planned it drained the event bus and
*then* checked the budget, so every event held at the ceiling was consumed and discarded — and
its own `budget.exhausted` notice was swallowed on the next tick. The budget check now runs
before the drain; over-budget events stay on the bus and dispatch when the hour rolls forward.

**The AWS mapping is documentation and was never deployed.** `docs/aws-mapping.md` is written
against the real ports, and it is explicit about the one thing that does not map cleanly:
merge-gate serialization is an `asyncio.Lock` locally, and `desired_count = 1` on Fargate is
not an equivalent guarantee during a rolling deploy. The honest production answer is a
DynamoDB conditional-write lock with a TTL. That is the first thing I would build if this
went further.

**The observed baseline** for the seeded simulator is a **0.4056** player win rate over 5000
rounds, with zero invariant violations — so the anomaly path depends on real drift rather
than on a pre-existing bug. If a demo needs the investigator to fire on cue, a labelled
`--inject-anomaly` switch would be the way to force it; it is deliberately not implemented,
because dead demo scaffolding is worse than a quiet sensor.

**What I would improve with more time**, in order: a `replay` command that re-runs a recorded
event against a worker for prompt iteration without waiting for a real trigger; per-worker
cost ceilings that adapt to observed spend rather than fixed constants; and a smarter cascade
policy than a depth cap — depth is a crude proxy for "is this loop still productive".
