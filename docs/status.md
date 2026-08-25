# Status: what works, what does not, what to do next

Measured from `agents/state/runs.jsonl` and `git log`, not from intent. See
[architecture.md](architecture.md) for how the pieces fit together.

---

## What works, with evidence

| Claim | Evidence |
|---|---|
| Agents land real commits on `main` | 7 agent commits: 6 reviews under `docs/reviews/`, 1 test file (`tests/test_cards.py`) |
| Triggers fire without a human | `commit.pushed` (post-commit hook + git poller), `coverage.gap` (coverage sensor), `deps.stale` (timer sensor via the PyPI API) all observed dispatching |
| The gate actually gates | A `dirty_main` refusal and a `scope_rejected` path both exercised; every merge ran the full suite first |
| The cascade is real | A merge wakes the reviewer; the reviewer's own commits are correctly skipped (`skipping self-authored commit c932a3a8`) |
| Cost is tracked honestly | $8.47 recorded across 15 runs; the hourly ceiling reads from the same ledger |
| It survives being killed | `Ctrl-C` twice mid-run; `recover()` reclaimed leases, closed run records, and cleared worktrees both times |

## What does not work yet

| Gap | Detail |
|---|---|
| **`dep-updater` has landed nothing** | Its work is *finished* and sitting on `agent/dep-updater-0b62a9fb` — it correctly determined `requests` is imported nowhere and removed the pin. Two early runs died at the old $0.40 cap; the third was dispatched at 21:49:56 and never got a worker slot before shutdown. See starvation, below. |
| **`anomaly-investigator` has never run** | Its trigger has never occurred. The simulator's baseline is a 0.4056 player win rate with zero invariant violations, so there is genuinely nothing anomalous to find. Unproven, not broken. |
| **Nothing is pushed** | `origin/main` is still at `Initial commit`; local is 35+ commits ahead. `PUSH_ENABLED` is `False` by design. |

## What to look into next, in priority order

**1. Worker starvation — this is the bug that matters.** `MAX_CONCURRENT_WORKERS = 2` and the
queue is a single FIFO. `commit.pushed` is by far the highest-volume trigger, and every merge
produces another one, so reviewers monopolise both slots indefinitely and every other worker
waits behind them. That is the whole reason `dep-updater` has no commits: it was dispatched
and simply never reached the front. `lease()` is one `SELECT ... WHERE state='queued' ORDER BY
created_at LIMIT 1` in `agents/adapters/sqlite_store.py` — the fix is to lease round-robin
across workers, or reserve a slot per worker class. Cheap change, and it is what stands
between this and all four agents demonstrably working.

**2. The reviewer's budget cap is still too tight.** One run ended `budget_exhausted` at
**$1.52 against a $1.50 cap**. Raising it is the obvious move, but the better question is why
reviews are that expensive: the reviewer is reviewing *the agent layer's own commits*, which
are large, rather than the small `casino/` diffs it was designed for. Consider scoping
`commit.pushed` to commits that touch `casino/`, which is both cheaper and closer to the
intended job.

**3. The layer spends most of its money reviewing itself.** 6 of 7 agent commits are reviews,
and reviews trigger reviews. The self-review filter now closes the direct loop, but the
economics still favour review over the work that changes the product. A per-worker or
per-trigger rate limit would rebalance it.

**4. Prove the `anomaly-investigator`.** The design anticipated this with an `--inject-anomaly`
switch, deliberately left unimplemented so there is no dead demo scaffolding. If it needs to
be shown working, that switch — clearly labelled — is the honest way, rather than pretending
drift occurred.

**5. Dead-lettered work is invisible.** 4 tasks are dead; `agents status` shows only the count.
There is no `agents dlq` command to list what was abandoned and why, and no way to requeue one
without editing SQLite by hand — which is exactly what had to be done to re-arm `dep-updater`
after its task was dead-lettered and `TimerSensor`'s announce-once meta key suppressed the
re-trigger. Both are one small CLI command away.

**6. Merge-gate serialization does not survive going to AWS.** See `docs/aws-mapping.md`. An
`asyncio.Lock` is airtight in one process; `desired_count = 1` on Fargate is not, because a
rolling deploy briefly runs two. The answer is a DynamoDB conditional-write lock with a TTL.

---


