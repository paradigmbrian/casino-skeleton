# What did not go as planned

Every item here is a defect found while building or running this layer, most of them
in the plan as written. The implementation plan's *Execution log*
(`superpowers/plans/2026-08-23-autonomous-agent-layer.md`) catalogues them by how each
was caught; the design spec's *Post-implementation corrections* covers the four safety
rails that did not hold.

---


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

**Nothing reclaimed an abandoned lease.** Interrupting the first live run exposed it: two
tasks sat in `leased` with no holder, and since the dedupe index spans queued *and* leased
rows, their work was permanently unqueueable — a silent, self-inflicted deadlock that only
showed up because the process was killed at the wrong moment. The AWS mapping had waved at
this ("SQS visibility timeout replaces the retry counter") without noticing that the local
adapter implemented no equivalent. `Supervisor.recover()` now runs at startup and a lease
timeout sweeps mid-flight, and the same interrupt also left orphaned worktrees and run
records stuck at `dispatched`, both of which are now cleaned up.

**The reviewer found an unbounded loop in its own trigger — and it was real.** The rule
"the reviewer never reviews its own commits" was implemented as a subject-prefix test for
`review:`. But the merge gate lands every branch with `git merge --no-ff`, so the reviewer's
work arrives on `main` under a subject *git* writes — `Merge branch 'agent/reviewer-x'` —
which sails past the filter. Reviewer reviews its own review, whose merge triggers another
review, forever; every merge carries a fresh SHA, so dedupe never breaks the cycle either.
The review documenting this replayed the filter over real history to show two turns of the
loop already in `main`, and noted the second-order cost: a reviewer handed a merge containing
only a markdown file has nothing in `casino/` to examine, so it either reports nothing or
reaches for marginal findings to justify the run.

Fixed by dereferencing the merge — judging a commit by the subjects it actually *introduces*,
not the one git generated — and by walking `--first-parent`, since without that a merged
branch was announced twice over (once for its own commits, once for the merge landing them)
and the same work was reviewed twice. The hook and the poller now share one predicate, which
was the review's own closing warning.

**The post-commit hook had never worked inside a worktree.** Git shares `.git/hooks` with
linked worktrees, so the hook fired on every worker's own commits, where `--show-toplevel`
points at the worktree and `.venv` does not exist. It failed silently there, which is why
nobody noticed — and which was lucky, because had it *succeeded* it would have announced
unmerged branch commits as having landed on `main`. It now exits quietly unless it is running
in the primary checkout. Also found by the reviewer.

**The budget guard silently stopped counting.** When a worker hits its per-run
`max_budget_usd`, the SDK raises `ResultError` *instead of* yielding a `ResultMessage` — so
the generic exception handler recorded a run that had spent its entire cap as costing
**$0.00**. Since the hourly ceiling is computed by summing those figures, the one rail that
bounds spend undercounted precisely when spending was highest, and could never trip. Caught
by watching a live run report `$0.72` while roughly `$1.90` had actually been spent. The
terminal result payload hangs off `ResultError.data`, so the real cost and turn count are
now recorded, and the run is classified `budget_exhausted` rather than a generic error.

Two consequences followed from the same bug. A capped task was being *retried*, spending its
ceiling a second time to reach the identical wall — one task cost twice its cap and produced
nothing — so runs that end at a limit are now dead-lettered instead. And a capped run was
discarded wholesale even when the agent had **already finished and committed valid work**:
one `dep-updater` run correctly removed the unused `requests` pin, then lost the change to a
cap that tripped afterwards. An agent's own commits now go to the gate however the run ended,
since the gate — scope check plus full suite — is what decides whether work is good. An
*uncommitted* worktree is still discarded; a run killed mid-edit may have left half a file.

**The recovery code deleted a directory it did not recognise.** `park_orphans()` scanned the
filesystem for children of the worktree root and treated each as a checkout — including the
merge gate's `integration/` directory, which is a *parent* of worktrees rather than one
itself, and which it then `rmtree`'d. It now asks `git worktree list --porcelain` what the
worktrees actually are, which also finds a gate worktree orphaned by a crash one level
deeper than the old scan looked.

**The `-v` flag was in the wrong place.** `agents up -v` — the form the README documents —
was an argparse error, because a flag declared on the top-level parser only binds *before*
the subcommand. It now works in either position.

**The AWS mapping is documentation and was never deployed.** `docs/aws-mapping.md` is written
against the real ports, and it is explicit about the one thing that does not map cleanly:
merge-gate serialization is an `asyncio.Lock` locally, and `desired_count = 1` on Fargate is
not an equivalent guarantee during a rolling deploy. The honest production answer is a
DynamoDB conditional-write lock with a TTL. That is the first thing I would build if this
went further.

**Per-run budgets were set too tight.** The one review that succeeded used **$0.72 of a
$0.75 cap** across 18 turns — it barely fit, and two sibling reviewers hit the wall. Caps are
now $1.50 (reviewer), $1.25 (test-author), $1.00 (investigator), $0.75 (dep-updater). The
`HOURLY_BUDGET_USD` is $10.00 — raised from $5.00 after a real run reached $4.78 in forty
minutes, which is close enough to the ceiling that the layer would have stalled mid-demo. Now
that accounting is honest the ceiling genuinely binds, so it is the single knob worth setting
deliberately before a long run.

**The observed baseline** for the seeded simulator is a **0.4056** player win rate over 5000
rounds, with zero invariant violations — so the anomaly path depends on real drift rather
than on a pre-existing bug. If a demo needs the investigator to fire on cue, a labelled
`--inject-anomaly` switch would be the way to force it; it is deliberately not implemented,
because dead demo scaffolding is worse than a quiet sensor.

**What I would improve with more time**, in order: a `replay` command that re-runs a recorded
event against a worker for prompt iteration without waiting for a real trigger; per-worker
cost ceilings that adapt to observed spend rather than fixed constants; and a smarter cascade
policy than a depth cap — depth is a crude proxy for "is this loop still productive".

---

See also: [status.md](status.md) for what to look into next.
