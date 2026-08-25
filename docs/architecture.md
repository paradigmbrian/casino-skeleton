# How the agent layer works

The design rationale behind `agents/`. For what it has actually done, see
[status.md](status.md); for what went wrong building it, see [postmortem.md](postmortem.md);
for the production story, see [aws-mapping.md](aws-mapping.md).

---

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

If the supervisor is killed mid-run — Ctrl-C, a crash, a closed laptop — the next start
recovers: abandoned leases are redelivered, run records frozen at `dispatched` are closed
out as `interrupted`, and orphaned worktrees are cleared. That reclaim matters more than it
looks: the dedupe index covers leased rows, so a task stuck in `leased` would otherwise block
that unit of work from ever being queued again.

A rejected branch is *parked*, not deleted: the worktree is removed but the branch survives,
so `git log agent/<worker>-<id>` shows exactly what an agent tried to do and the ledger says
why it was refused.


---

See also: [running.md](running.md) for the commands, and [aws-mapping.md](aws-mapping.md)
for how each local component maps to a managed service.
