# AWS mapping

**Nothing in this document was deployed.** No AWS resources were created, no `boto3` appears
on the demo path, no `terraform apply` was run, and no cloud spend was incurred. This is the
production story for a layer that currently runs entirely on one laptop, written down so the
seams are inspectable rather than hypothetical.

The reason it is a mapping rather than a rewrite is `agents/ports.py`. `EventBus`,
`WorkQueue`, and `RunStore` are abstract; `agents/adapters/sqlite_store.py` is one
implementation of all three. Everything above the ports — orchestrator, supervisor, merge
gate, worker runtime, sensors — depends only on the abstract interfaces. Moving to AWS means
writing three new adapters, not touching the control plane.

## Component by component

| Local | AWS | Notes |
|---|---|---|
| `timer_sensor` (90s asyncio tick) | EventBridge Scheduler | A schedule expression publishing directly to the bus. The PyPI lookup moves into a small Lambda so the trigger stays deterministic. |
| `git_sensor` (10s `git log` poll) | GitHub webhook → API Gateway → Lambda | Push-driven, so polling disappears. The local `hooks/post-commit` is the same idea at laptop scale. |
| `coverage_sensor` | CodeBuild project on a schedule → EventBridge | Coverage needs a full checkout and a test run; that is a build job, not a Lambda. |
| `anomaly_sensor` | CloudWatch custom metric + alarm → EventBridge | The simulator emits `PlayerWinRate` as a metric; an anomaly-detection alarm replaces the hand-rolled z-test. Invariant checks stay as code because they are assertions, not statistics. |
| `sim_runner` | ECS Scheduled Task | Same container image as the workers, different entrypoint. |
| `EventBus` (`events` table) | EventBridge custom bus | `config.ROUTES` becomes a set of EventBridge rules — one rule per event type, target = the worker's queue. The routing table stops being application code and becomes infrastructure. |
| `WorkQueue` (`tasks` table + partial unique index) | SQS + DLQ | `lease` → `ReceiveMessage` with a visibility timeout; `ack` → `DeleteMessage`; `nack` → let visibility expire. The redrive policy replaces `MAX_TASK_ATTEMPTS`. **The local adapter had to grow its own visibility timeout** (`reclaim_expired_leases`) — see below. Dedupe uses a FIFO queue with `MessageDeduplicationId` = the dedupe key. |
| orchestrator + merge gate | ECS Fargate service | Long-lived, needs a git checkout, holds the lock described below. |
| workers | ECS Fargate `RunTask` per lease | One task per run, torn down after. |
| git worktree isolation | The task's own ephemeral volume | A fresh `git clone --depth` per task is simpler in the cloud than worktrees and gives the same isolation. |
| `RunStore` (`runs` table + `runs.jsonl`) | DynamoDB + CloudWatch Logs | `run_id` as partition key; the JSONL mirror becomes structured log events. `cost_since` becomes a GSI on `started_at`. |
| `ANTHROPIC_API_KEY` from the environment | Secrets Manager | Injected as a task secret, never in the image or task definition. |
| `agents/state/STOP` flag file | SSM Parameter Store flag, checked each loop | Same kill switch, one that survives a task restart. |

## Why workers are Fargate, not Lambda

Three properties of a worker run rule Lambda out:

1. **Git.** A worker needs a real checkout it can branch, edit, and commit in. Lambda's `/tmp`
   is 512 MB–10 GB of ephemeral scratch with no git binary; you would be shipping git in a
   layer and re-cloning on every cold start.
2. **Runtime.** `WorkerSpec.timeout_s` is 300–420 seconds today, and that is a deliberate cap,
   not a natural ceiling — a reviewer reading a large diff can legitimately want longer. Lambda
   stops at 15 minutes, which is close enough to the working range to be a design constraint
   rather than a comfortable margin.
3. **Filesystem persistence within a run.** The agent writes files, runs `pytest` against them,
   reads the output, and iterates. That is a workstation workload wearing a container.

The orchestrator and merge gate are a *service* rather than per-invocation tasks for a
different reason: they hold state (the lock, the leased-task view) and they must be singular.

## Where relying on the mapping bit

An earlier draft of this document listed the SQS visibility timeout as the production
equivalent of the local lease, which is accurate — and hid the fact that the local adapter
implemented no equivalent at all. `lease()` selects only `queued` rows, so a task leased by a
supervisor that then died stayed leased forever; and because the dedupe index spans both
`queued` and `leased`, that unit of work could never be enqueued again. A silent deadlock,
found by killing a live run with Ctrl-C.

The lesson is narrow but worth stating: a mapping table describes what a managed service
*would* do for you, and it is easy to read that as a description of what the local code
*does*. `reclaim_expired_leases` plus `Supervisor.recover()` now provide the property
locally, and the AWS row is genuinely a swap rather than an upgrade.

## The one thing that does not map cleanly

Merge-gate serialization.

Locally, "only one thing writes to `main` at a time" is an `asyncio.Lock` in a single process
(`Supervisor._gate_lock`). That guarantee is airtight because there is exactly one process.

On Fargate, `desired_count = 1` looks like the equivalent and is not. During a rolling
deployment ECS starts the replacement task before draining the old one, so there is a window
— seconds, but real — where two gate processes exist. Two concurrent `git merge` operations
against the same branch is precisely the failure the gate exists to prevent, and it would show
up as a corrupted merge under load, i.e. rarely and unreproducibly.

The correct production answer is an explicit distributed lock, not a deployment setting:

```
PutItem  Table=agent-locks
         Item={lock_id: "merge-gate", holder: <task-arn>, expires_at: <now + 120s>}
         ConditionExpression="attribute_not_exists(lock_id) OR expires_at < :now"
```

A conditional write is atomic in DynamoDB, so exactly one task wins. The TTL matters as much
as the condition: a worker that dies mid-merge must not hold the lock forever, and 120 seconds
comfortably exceeds a gate cycle (scope check, merge, test run, fast-forward). The holder
renews while it works and deletes on exit; a crashed holder is reclaimed when `expires_at`
passes.

This is the piece I would write first if this went to production, because it is the one place
where the local design's correctness argument does not survive the move.

## What else would need to change

- **Cost accounting.** `HOURLY_BUDGET_USD` is enforced by summing `runs.cost_usd` in SQLite.
  In AWS that becomes a DynamoDB query against the `started_at` GSI, and the ceiling should
  additionally be backstopped by an AWS Budgets action that disables the EventBridge rules —
  application-level budget guards do not survive application-level bugs.
- **Secret hygiene in the ledger.** `RunRecord` carries status, cost, file paths, and the
  agent's own summary text. Nothing in that set is a secret today, but a summary is model
  output and model output can quote anything the agent read. Shipping it to CloudWatch means
  it lands in a durable, widely-readable store, so a redaction pass on `summary` becomes
  necessary at that point in a way it is not locally.
- **Observability.** `agents status` and `agents events` read SQLite directly. The cloud
  equivalent is a CloudWatch dashboard over the same fields, plus an alarm on DLQ depth,
  which is the single most informative "the layer is unhealthy" signal.
