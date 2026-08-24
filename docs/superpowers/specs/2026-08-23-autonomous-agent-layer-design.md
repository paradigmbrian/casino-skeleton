# Autonomous Agent Layer — Design

**Date:** 2026-08-23
**Repo:** casino-skeleton
**Context:** Agent Engineer take-home (`docs/Agent Engineer Take-Home.docx.pdf`)
**Status:** Approved design, pending implementation plan

---

## 1. Problem

The starter repo is a ~200 LOC blackjack simulator. The casino is not the deliverable. The
deliverable is a layer of agents that maintain the repo the way a production codebase gets
maintained — reviewing changes, writing tests, updating docs, keeping dependencies current —
firing on their own triggers with no human in the loop.

### Requirements from the brief

| # | Requirement |
|---|---|
| R1 | At least 3 agents that proactively modify the repo (real SDLC work) |
| R2 | Each agent has its own trigger — commit, timer, failing test, output anomaly — not a manual go-ahead |
| R3 | The layer runs unattended for a stretch of the screen recording |
| R4 | Agents' commits appear in the repo history alongside the template's original commit |
| R5 | A README covering: how to run it, each agent and its trigger, AI tools used, what did not go as planned |
| R6 | 2 hours total including setup; reasoning and prioritization over polish |

### Graded on

How you decide what is worth automating; how quickly and cleanly you stand up working agents;
**how transparent the system is to someone who was not watching it run**; production-ready
instincts; whether the agents do real work on a non-human trigger.

### Seeded affordances found in the starter repo

The template is deliberately salted with maintenance work:

| Seed | Location | Agent it invites |
|---|---|---|
| `requests==2.6.0` — 2015 release, known CVEs, imported nowhere | `requirements.txt` | dependency updater |
| Five modules at 0% coverage (`table`, `cards`, `strategies`, `monitor`, `simulate`) | `casino/` | test author |
| Three-line README | `README.md` | docs agent (not selected) |
| `"""Bare-bones outcome logger. No aggregation, no dashboard -- extend this."""` | `casino/monitor.py:8` | anomaly investigator |

Latent bugs available as review fodder:

- `Hand.is_blackjack()` is dead code — `Table._outcome` never consults it, so naturals are
  neither detected nor paid, and a natural 21 versus a drawn 21 resolves as a push.
- `Table.play_round` builds a fresh `Deck` every round, so `num_decks` is cosmetic, there is no
  shoe or penetration, and card counting is impossible by construction.
- `Deck.draw()` pops an empty list. Unreachable at 52 cards; reachable the moment a shoe exists.
- `BasicPlayerStrategy.should_hit` accepts `dealer_upcard` and ignores it.
- No bankroll or betting, so there is no house-edge signal.

---

## 2. Decisions

Recorded with rationale, because the reasoning is what is being graded.

| # | Decision | Chosen | Rejected | Why |
|---|---|---|---|---|
| D1 | Deployment target | Local run, AWS-ready design | Real AWS deploy; hybrid | 2h budget. IAM, secrets and webhook plumbing would consume the entire clock and produce fewer working agents. |
| D2 | Worker runtime | `claude-agent-sdk` (Python) | Tool Runner; OpenHands SDK | Ships Read/Write/Edit/Bash/Grep and the agent loop. Workers must run pytest and git — hand-writing those tools costs 30–40 min for no differentiation. |
| D3 | Architecture pattern | Orchestrator + workers | Independent scripts | Cross-agent routing (one agent's finding becomes another's task) is the point. |
| D4 | Dispatch | Deterministic router | LLM planner; hybrid triage | Control plane must be reproducible and greppable. Transparency is a graded criterion; a nondeterministic control plane undercuts it. All intelligence lives in workers. |
| D5 | Worker isolation | Worktree + branch per worker, gated merge | Single-writer lock; PR-with-human-merge | Answers the obvious "what if two agents edit the same file" question. PR flow would leave no agent commits on `main`, violating R4. |
| D6 | Queue / bus substrate | SQLite behind port ABCs | LocalStack; in-memory asyncio | Durable across restarts, zero dependencies, inspectable live with the `sqlite3` CLI during the recording. |
| D7 | Roster | 4 workers (below) | 3, or adding a docs agent | Four distinct trigger classes; docs work is absorbed by the workers' own write scopes. |
| D8 | AWS artifacts | Ports + `docs/aws-mapping.md` | Writing `sqs_queue.py` etc. | Unexercised cloud adapters with no test and no deployment read as padding. The ABC plus a working SQLite implementation proves the seam. |

---

## 3. Architecture

Three planes. The intelligent plane is small; the auditable plane is deterministic.

```
 SENSING (no LLM)                    CONTROL (deterministic)          EXECUTION (LLM)
 ───────────────                     ───────────────────────          ───────────────
 timer_sensor     ─┐                                                  ┌─ dep-updater
 git_sensor       ─┤                 ┌───────────────┐                ├─ test-author
 coverage_sensor  ─┼──> EventBus ──> │  ORCHESTRATOR │ ──> WorkQueue ─┼─ reviewer
 anomaly_sensor   ─┘    (sqlite)     │  routing table│     (sqlite)   └─ anomaly-investigator
 sim_runner       ─┘                 │  dedupe       │                      │
                                     │  budget guard │                      │ each in its own
                                     │  cascade cap  │                      │ worktree + branch
                                     │  MERGE GATE   │ <────────────────────┘
                                     └───────┬───────┘   branch + result
                                             │
                                  scope check, then pytest
                                    green ─> ff-merge main ─> emits commit.pushed ──┐
                                    red   ─> park branch, emit test.failed          │
                                                                                    │
                                     └──────────────────────────────────────────────┘
                                              cascade, depth-capped at 3
```

### Control flow

1. **Sensors** poll on their own cadence and publish typed events. A sensor is a pure function
   returning `list[Event]`. No LLM in this plane.
2. **Orchestrator** drains the event table, applies the routing table, enqueues tasks with a
   dedupe key so the same signal cannot be queued twice while one is in flight.
3. **Workers** lease with bounded concurrency (2). Each gets a fresh worktree and branch off
   `main`, runs `claude-agent-sdk` under a tool allowlist, and commits inside its worktree.
   No worker ever writes to `main`.
4. **Merge gate** is the single writer and is strictly serialized. It verifies the branch diff
   against the worker's declared write scope, merges to an integration ref, runs the full suite,
   and fast-forwards `main` only if green. Red parks the branch and emits `test.failed` — which
   routes back to `test-author`, so a bad agent commit becomes another agent's input rather than
   a broken repo.
5. Every step appends to a run ledger (SQLite, mirrored to JSONL).

### Routing table

| Event | Worker |
|---|---|
| `commit.pushed` | reviewer |
| `coverage.gap` | test-author |
| `test.failed` | test-author |
| `regression.needed` | test-author |
| `deps.stale` | dep-updater |
| `outcome.anomaly` | anomaly-investigator |
| `outcome.invariant_violation` | anomaly-investigator |
| `review.fix_requested` | reviewer |

---

## 4. Workers

One runtime, four specs. The differences are data:

```python
@dataclass(frozen=True)
class WorkerSpec:
    name: str
    triggers: tuple[str, ...]        # event types this worker accepts
    system_prompt: str
    allowed_tools: tuple[str, ...]   # Agent SDK tool allowlist
    write_scope: tuple[str, ...]     # path globs, enforced at the merge gate
    max_turns: int = 25
    timeout_s: int = 300
    max_cost_usd: float = 0.50
```

### Write scopes are disjoint

| Worker | Trigger | May write | May not write |
|---|---|---|---|
| `dep-updater` | `deps.stale` (timer) | `requirements.txt`, `docs/dependencies.md` | source, tests |
| `test-author` | `coverage.gap`, `test.failed`, `regression.needed` | `tests/**` | **`casino/**`** |
| `reviewer` | `commit.pushed`, `review.fix_requested` | `casino/**`, `docs/reviews/**` | **`tests/**`** |
| `anomaly-investigator` | `outcome.anomaly`, `outcome.invariant_violation` | `docs/investigations/**` | everything else |

Two consequences justify the constraint:

1. **Merge conflicts between workers are structurally impossible.** The gate verifies rather
   than resolves, which is what keeps parallel worktrees cheap.
2. **The two degenerate behaviours are ruled out by construction, not by prompt instruction.**
   `test-author` cannot make a failing test pass by editing the source. `reviewer` cannot make a
   failing suite green by weakening a test.

Enforcement is at the gate: `git diff --name-only main...branch` is checked against
`write_scope` before tests run, and out-of-scope commits are rejected outright. The prompt-level
instruction is a hint; the gate is the control.

### Consequence: the investigator is diagnose-only

Because `docs/investigations/**` is its entire write scope, `anomaly-investigator` cannot fix
what it finds. It writes the investigation, then emits `review.fix_requested` (routed to
reviewer, who owns `casino/`) and `regression.needed` (routed to test-author, who owns
`tests/`). One agent's finding becomes two agents' tasks — the orchestrator-worker pattern
earning its keep rather than being four cron jobs in a trenchcoat.

### Models

Workers run `claude-opus-5` with adaptive thinking at `effort: "high"`. The control plane makes
zero LLM calls. Sensors are pure Python.

---

## 5. Sensors

| Sensor | Cadence | Emits | Mechanism |
|---|---|---|---|
| `git_sensor` | hook + 10s poll | `commit.pushed` | `post-commit` hook publishes immediately; poller reconciles against last-seen SHA |
| `coverage_sensor` | on `commit.pushed` | `coverage.gap` | `coverage run -m pytest && coverage json`, per-module threshold 80% |
| `timer_sensor` | 90s | `deps.stale` | reads pins, queries the PyPI JSON API, diffs versions |
| `sim_runner` | 120s | (none) | re-runs `python -m casino.simulate` to keep fresh outcomes arriving |
| `anomaly_sensor` | on each sim batch | `outcome.anomaly`, `outcome.invariant_violation` | tails `outcomes.jsonl` |

**Push plus reconcile on git.** The hook gives a sub-second trigger on camera; the poller catches
commits made while the layer was down. Double delivery is harmless — the dedupe key is the SHA.
It also means the demo does not silently break if hook installation did not take.

**The PyPI lookup lives in the sensor, not the agent.** The sensor emits `{package, pinned,
latest}` as data; the agent exercises judgment on what to do with it — bump the pin, or notice
`requests` is imported nowhere and propose removing it. Keeping the network call deterministic
means the trigger fires reliably even if a model call flakes.

### Anomaly detection

Two independent checks.

1. **Invariant violations** — `winner == "player"` while `player_value > 21`; a `winner` outside
   the enum; values outside `[4, 30]`. These are logic bugs, not noise. The check must be
   *winner-conditional*: busts legitimately log raw values above 21, which is why the existing
   `outcomes.jsonl` contains `"dealer_value": 25` and why a naive `value > 21` test would fire on
   roughly every other row.
2. **Distribution drift** — on first start the layer runs 5,000 rounds and stores the empirical
   player win rate as a baseline, rather than hardcoding a guessed constant. Each subsequent
   200-round batch gets a two-proportion z-test against it; `|z| > 3` emits `outcome.anomaly`
   carrying both rates, both sample sizes, and the z-score.

**Where an anomaly legitimately comes from.** `reviewer` owns `casino/`, and the seeded dead-code
bug is `is_blackjack()`. If reviewer fixes it, naturals start being detected, the win rate
genuinely moves, drift fires, the investigator diagnoses it, and a regression test is routed to
test-author. Full loop, no theater.

That path is not guaranteed to occur inside a 20-minute recording, since it depends on reviewer
selecting that particular finding. A `--inject-anomaly` development flag swaps in a degenerate
strategy as a deterministic fallback. It is labelled in the README as a demo aid and is not part
of the system.

---

## 6. Safety rails

| Rail | Mechanism |
|---|---|
| Cascade cap | Every event carries `depth`; the orchestrator drops anything past 3. Reviewer never reviews its own commits, but does review other agents' — that is the cascade worth showing. |
| Budget guard **†** | Per-run `max_cost_usd` plus a global per-hour ceiling. Exceeded means dispatch is refused and `budget.exhausted` is emitted; the layer keeps running rather than silently burning money. |
| Timeouts and DLQ **†** | Wall-clock kill per run, branch parked, task retried N times then dead-lettered with the failure recorded. |
| Idempotency **†** | Dedupe key per task; the same coverage gap cannot be queued twice while one is in flight. |
| Kill switch **†** | `python -m agents.cli stop` sets a flag checked between dispatches; SIGTERM drains in-flight work rather than orphaning worktrees. |
| Scope enforcement **†** | Diff checked against `write_scope` at the gate, before tests. |
| Push is opt-in | `PUSH_ENABLED` flag. The gate always merges locally; pushing to `origin` is the one action that leaves the machine. |
| Secret hygiene | `ANTHROPIC_API_KEY` from the environment, never logged; the ledger redacts. |

**†** These rails did not hold as designed. See *Post-implementation corrections* at the end
of this document.

---

## 7. Repo layout

```
agents/
  cli.py                    up | status | events | stop
  config.py                 routing table, thresholds, cadences, budgets
  supervisor.py             asyncio: sensor loop + orchestrator loop + worker pool
  orchestrator.py           router, dedupe, budget guard, cascade cap
  merge_gate.py             single writer to main: scope check -> pytest -> ff-merge
  worktree.py               worktree/branch lifecycle, park and cleanup
  worker.py                 generic runtime wrapping claude-agent-sdk
  specs/
    reviewer.py  test_author.py  dep_updater.py  anomaly_investigator.py
  sensors/
    git.py  coverage.py  timer.py  anomaly.py  sim_runner.py
  ports/                    WorkQueue | EventBus | RunStore  (ABCs)
  adapters/
    sqlite_queue.py  sqlite_bus.py  sqlite_runstore.py
  requirements.txt          claude-agent-sdk, coverage   (NOT the root file)
  state/                    agents.db, runs.jsonl        (gitignored)
hooks/post-commit
docs/aws-mapping.md
```

The agent layer's own dependencies live in `agents/requirements.txt`, deliberately not the root
`requirements.txt` — that file is `dep-updater`'s write scope, and the agent must not bump the
SDK out from under the running layer.

### Runner surface

```
python -m agents.cli up               # supervisor: sensors + orchestrator + bounded worker pool
python -m agents.cli status           # queue depth, in-flight runs, budget spent, recent outcomes
python -m agents.cli events --follow  # live event tail
python -m agents.cli stop             # kill switch, drains in-flight work
```

Structured JSON logs to stdout plus the mirrored `runs.jsonl` ledger. Together with the
`sqlite3`-inspectable state, this is the answer to "transparent to someone who was not watching
it run."

---

## 8. What runs where

Everything runs locally. No AWS resources are created, no boto3 in the demo path, no
`terraform apply`, no cloud spend.

Three things do leave the machine, none of them AWS:

1. **Anthropic API calls** — every worker run is a real billed request. The budget guard bounds it.
2. **PyPI** — `timer_sensor` queries the JSON API for latest versions.
3. **`git push`** — off by default behind `PUSH_ENABLED`.

### AWS mapping (documentation only)

Ports exist so this is a config change rather than a rewrite. Full detail lives in
`docs/aws-mapping.md`.

| Local | AWS |
|---|---|
| `timer_sensor` | EventBridge Scheduler |
| `git_sensor` | GitHub webhook -> API Gateway -> Lambda (replaces polling) |
| `anomaly_sensor` | CloudWatch custom metric + alarm -> EventBridge |
| `EventBus` | EventBridge custom bus; rules encode the routing table |
| `WorkQueue` | SQS + DLQ; redrive policy replaces the retry counter |
| orchestrator + gate | ECS Fargate service |
| workers | ECS Fargate `RunTask` per lease |
| `RunStore` | DynamoDB + CloudWatch Logs |
| API keys | Secrets Manager |

Workers are Fargate rather than Lambda because they need git, a writable filesystem, and
multi-minute runs; Lambda's 15-minute ceiling and ephemeral `/tmp` are the wrong shape.

**The one thing that does not map cleanly:** merge-gate serialization. Locally it is a single
process holding a lock. `desired_count = 1` on Fargate is not a hard singleton guarantee — a
rolling deploy can briefly run two. The correct answer is a conditional-write lock in DynamoDB
with a TTL, and `docs/aws-mapping.md` specifies that.

---

## 9. Scope and time

Built by hand this design is 3–4 hours, not 2. What makes the budget plausible is that the brief
explicitly permits AI tooling for building the solution, and directing Claude Code through it is
materially faster than typing it.

### Cut list, in sacrifice order

1. `replay` command
2. `docs/aws-mapping.md` reduced to the summary table in the README
3. `anomaly-investigator` — roster drops to 3, still clears R1
4. Worktree isolation -> single-writer lock (loses parallelism, keeps correctness)
5. Budget guard -> hard task-count ceiling

### Non-negotiable floor

Three agents with genuinely distinct triggers; the merge gate running tests before anything lands
on `main`; the run ledger; the README. That set satisfies every stated requirement.

---

## 10. Success criteria

- [ ] Layer starts with one command and runs unattended
- [ ] At least 3 workers fire on non-human triggers and land real commits on `main`
- [ ] Agent commits are visible in `git log` alongside the template's `Initial commit`
- [ ] No commit lands on `main` without the full suite passing
- [ ] A reviewer who was not watching can reconstruct what happened from the ledger alone
- [ ] Cascade is visible: one agent's commit triggers another agent's work
- [ ] README covers run instructions, each agent and trigger, AI tools used, and what did not go
      as planned


---

## Post-implementation corrections

This section is appended after building and running the layer. The design above is left as
written — it is the record of what was intended — and this records where that intent was
wrong. Full detail, including the fixes, is in the implementation plan's *Execution log* and
the README's *what did not go as planned*.

**Budget guard (§6).** The design treated per-run `max_cost_usd` and the hourly ceiling as
independent rails. They are not: hitting the per-run cap makes the SDK raise instead of
returning a result, so the run's cost was recorded as `$0.00` and the hourly ceiling — which
is computed by summing recorded costs — undercounted by exactly the amount most worth
counting. A rail that is derived from another rail's output fails whenever that output has a
failure mode, and this one failed silently and in the unsafe direction. Cost is now read from
the terminal error payload.

**Timeouts and DLQ (§6).** "Task retried N times then dead-lettered" is wrong for a run that
ended at a *limit*. A retry re-spends the same budget to reach the same wall; the design
implicitly assumed every failure is transient. Failures now split into terminal
(`budget_exhausted`, `max_turns`, `scope_rejected` — dead-lettered immediately) and
retryable.

Relatedly, "branch parked" discarded work in the case where the agent had finished and
committed *before* the limit tripped. Parking was modelled as the failure path, but a run
that hit a ceiling is not necessarily a run that produced nothing. An agent's own commits now
go to the gate regardless of how the run ended — the gate, not the run's exit status, is what
decides whether work is good.

**Idempotency (§6).** The dedupe rail — a partial unique index over `('queued', 'leased')` —
is correct while a supervisor is alive and became a deadlock when one died. A task left in
`leased` by a killed process is never re-leased, and its dedupe key blocks that unit of work
forever. The design named SQS as the production equivalent without noticing that the property
being relied on (visibility-timeout redelivery) had no local implementation. A lease timeout
and a startup reclaim now provide it.

**Kill switch (§6).** "SIGTERM drains in-flight work rather than orphaning worktrees" is
simply false as built — Ctrl-C orphaned both worktrees and leases. Rather than add signal
handling, recovery moved to startup, which is strictly more robust: it also covers a power
loss or a `kill -9`, neither of which any handler can catch. The trade-off is that wreckage
is visible between the crash and the next start.

**Scope enforcement (§6).** The design listed only the gate-side check. The tool-call-time
`can_use_tool` guard added in the plan turned out not to fire at all, because listing a write
tool in `allowed_tools` auto-approves it before the callback runs. Had the design's
gate-only rail been the whole story this would not have mattered; because the plan advertised
prevention *and* detection while delivering only detection, it was worth catching. Both layers
now work, and they are not redundant: `Bash` is pre-approved and can write via a shell
redirect, which only the gate sees.

**What held.** Disjoint write scopes, the merge gate's scope-then-tests ordering, the
deterministic routing table, worktree isolation, and the ledger all behaved as designed. The
cascade was observed working end to end: a hook-published commit woke the reviewer, its
`review:` commit was correctly skipped by the git sensor rather than triggering a self-review
loop.
