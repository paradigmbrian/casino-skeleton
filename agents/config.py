from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "agents" / "state"
DB_PATH = STATE_DIR / "agents.db"
LEDGER_PATH = STATE_DIR / "runs.jsonl"
STOP_FLAG = STATE_DIR / "STOP"
WORKTREE_ROOT = REPO_ROOT.parent / f"{REPO_ROOT.name}-worktrees"

# Deterministic dispatch. The whole control plane is this dict.
ROUTES: dict[str, str] = {
    "commit.pushed": "reviewer",
    "review.fix_requested": "reviewer",
    "coverage.gap": "test-author",
    "test.failed": "test-author",
    "regression.needed": "test-author",
    "deps.stale": "dep-updater",
    "outcome.anomaly": "anomaly-investigator",
    "outcome.invariant_violation": "anomaly-investigator",
}

MAX_CASCADE_DEPTH = 3
MAX_CONCURRENT_WORKERS = 2
MAX_TASK_ATTEMPTS = 2
# Comfortably above the longest WorkerSpec.timeout_s (420s) plus a gate cycle,
# so a slow-but-alive worker is never redelivered underneath itself.
LEASE_TIMEOUT_S = 900

# Budget. Per-run is enforced natively by the SDK; hourly is ours.
HOURLY_BUDGET_USD = 5.00

# Sensor cadences, seconds.
GIT_POLL_S = 10
TIMER_S = 90
SIM_RUNNER_S = 120

COVERAGE_THRESHOLD = 80.0
ANOMALY_Z_THRESHOLD = 3.0
ANOMALY_BASELINE_ROUNDS = 5000
ANOMALY_BATCH_ROUNDS = 200

PUSH_ENABLED = False  # the one action that leaves this machine
MAIN_BRANCH = "main"
