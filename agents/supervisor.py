from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from agents import config
from agents.adapters.sqlite_store import SqliteStore
from agents.logfmt import event_kwargs
from agents.merge_gate import MergeGate
from agents.orchestrator import Orchestrator, parse_handoffs
from agents.sensors.anomaly import AnomalySensor
from agents.sensors.coverage import CoverageSensor
from agents.sensors.git import GitSensor
from agents.sensors.sim_runner import run_simulation
from agents.sensors.timer import TimerSensor
from agents.specs import SPECS, task_brief
from agents.types import RunRecord, Task, utcnow
from agents.worker import ensure_committed, run_agent
from agents.worktree import WorktreeManager

LOG = logging.getLogger("agents.supervisor")

# Statuses where another attempt cannot help. A run that hit its budget or turn
# ceiling spends the same ceiling reaching the same wall; a scope rejection is a
# deterministic policy violation, not bad luck. `tests_failed` is deliberately
# absent -- that one publishes test.failed and is worth a second look.
TERMINAL_STATUSES = frozenset({"budget_exhausted", "max_turns", "scope_rejected"})


class Supervisor:
    def __init__(self, store, orchestrator, gate, worktrees, specs, repo_root: Path,
                 agent_runner=None, sensors=None, max_concurrency: int = 2):
        self.store = store
        self.orchestrator = orchestrator
        self.gate = gate
        self.worktrees = worktrees
        self.specs = specs
        self.repo_root = Path(repo_root)
        self.agent_runner = agent_runner or run_agent
        self.sensors = sensors or []
        self.max_concurrency = max_concurrency
        self.stopping = False
        self._gate_lock = asyncio.Lock()  # the single-writer guarantee

    def request_stop(self) -> None:
        self.stopping = True

    def recover(self) -> None:
        """Undo a hard exit. A supervisor killed mid-run leaves three kinds of
        wreckage: tasks stuck in `leased` (which the dedupe index then blocks
        forever), worktrees nobody owns, and run records frozen at 'dispatched'.
        Called at startup, where every leased task is by definition abandoned --
        no run can be in flight in a process that has not started yet."""
        requeued = self.store.reclaim_expired_leases(
            timeout_s=0, max_attempts=config.MAX_TASK_ATTEMPTS)
        if requeued:
            LOG.info("", extra=event_kwargs("recover", "leases",
                                            f"{len(requeued)} abandoned, requeued"))

        closed = self.store.close_dangling_runs()
        if closed:
            LOG.info("", extra=event_kwargs("recover", "runs",
                                            f"{len(closed)} interrupted, closed out"))

        parked = self.worktrees.park_orphans()
        if parked:
            LOG.info("", extra=event_kwargs("recover", "worktrees",
                                            f"parked {', '.join(parked)}"))

    # ------------------------------------------------------------------ run one

    async def execute(self, task: Task) -> RunRecord:
        spec = self.specs[task.worker]
        run_id = uuid.uuid4().hex[:12]
        wt = self.worktrees.create(task.worker)

        record = RunRecord(run_id=run_id, worker=spec.name, event_type=task.event.type,
                           task_id=task.id, branch=wt.branch, status="dispatched",
                           started_at=utcnow())
        self.store.record(record)
        LOG.info("", extra=event_kwargs("start", spec.name, wt.branch))

        outcome = await self.agent_runner(spec, task_brief(task.event), wt.path)

        def finish(status: str, files=(), error=None) -> RunRecord:
            final = RunRecord(
                run_id=run_id, worker=spec.name, event_type=task.event.type, task_id=task.id,
                branch=wt.branch, status=status, started_at=record.started_at,
                ended_at=utcnow(), cost_usd=outcome.cost_usd, num_turns=outcome.num_turns,
                files_changed=tuple(files), summary=outcome.summary, error=error,
            )
            self.store.record(final)
            kind = {"merged": "merged", "budget_exhausted": "budget",
                    "scope_rejected": "rejected", "max_turns": "budget"}.get(status, "failed")
            files = ("  " + ", ".join(final.files_changed[:2])) if final.files_changed else ""
            LOG.info("", extra=event_kwargs(
                kind, spec.name,
                f"${outcome.cost_usd:.2f}  {outcome.num_turns:>2}t{files}"))
            return final

        # A run can end at a limit *after* the agent finished and committed --
        # observed live, where a budget cap discarded a complete, correct change.
        # Its own commits are offered to the gate however the run ended; the gate
        # checks scope and runs the suite, which is what decides whether work is
        # good. What is not offered is an uncommitted worktree: a run killed
        # mid-edit may have left half a file, and that is not work.
        clean_finish = outcome.status == "agent_done"
        if not clean_finish and self.worktrees.commits_ahead(wt.branch) == 0:
            self.worktrees.retire(wt)
            return finish(outcome.status, error=outcome.error)
        if not clean_finish:
            LOG.info("", extra=event_kwargs(
                "salvaged", spec.name,
                f"ended {outcome.status} but had committed work; gating it anyway"))

        # Cross-worker handoffs are published whether or not the merge succeeds:
        # the investigator's value is the finding, not the file it wrote.
        for event in parse_handoffs(outcome.summary, task.event):
            self.store.publish(event)

        if clean_finish:
            await asyncio.to_thread(ensure_committed, wt.path,
                                    f"{spec.name}: automated change ({task.event.type})")

        async with self._gate_lock:
            result = await asyncio.to_thread(self.gate.submit, wt.branch, spec.write_scope)

        if result.status == "merged":
            self.worktrees.cleanup(wt)
            return finish("merged", result.changed_files,
                          error=None if clean_finish else outcome.error)

        self.worktrees.retire(wt)
        if result.status == "tests_failed":
            self.store.publish(task.event.child(
                "test.failed", {"branch": wt.branch, "detail": result.detail}))
        return finish(result.status, result.changed_files, error=result.detail)

    async def settle(self, task: Task, status: str) -> None:
        """Decide the task's fate from how its run ended."""
        if status == "merged":
            self.store.ack(task.id)
            return
        if status in TERMINAL_STATUSES:
            LOG.info("", extra=event_kwargs(
                "failed", task.worker, f"{status}; dead-lettered (a retry hits the same wall)"))
            self.store.nack(task.id, max_attempts=0)  # 0 => terminal on this attempt
            return
        LOG.info("", extra=event_kwargs("failed", task.worker, f"{status}; requeued"))
        self.store.nack(task.id, config.MAX_TASK_ATTEMPTS)

    # ------------------------------------------------------------------ loops

    async def _sensor_loop(self) -> None:
        clocks = {id(s): 0.0 for s, _ in self.sensors}
        while not self.stopping:
            now = asyncio.get_running_loop().time()
            for sensor, interval in self.sensors:
                if now - clocks[id(sensor)] < interval:
                    continue
                clocks[id(sensor)] = now
                try:
                    events = await asyncio.to_thread(sensor.poll)
                except Exception:
                    LOG.exception("sensor %s failed", type(sensor).__name__)
                    continue
                for event in events:
                    self.store.publish(event)
                if events:
                    LOG.info("", extra=event_kwargs(
                        "sensor", type(sensor).__name__,
                        f"{len(events)} event(s): " + ", ".join(
                            sorted({e.type for e in events}))))
            await asyncio.sleep(1.0)

    async def _sim_loop(self) -> None:
        while not self.stopping:
            await asyncio.to_thread(run_simulation, self.repo_root, config.ANOMALY_BATCH_ROUNDS)
            await asyncio.sleep(config.SIM_RUNNER_S)

    async def _dispatch_loop(self) -> None:
        while not self.stopping:
            try:
                self.store.reclaim_expired_leases(config.LEASE_TIMEOUT_S,
                                                  config.MAX_TASK_ATTEMPTS)
                self.orchestrator.dispatch_pending()
            except Exception:
                LOG.exception("dispatch failed")
            await asyncio.sleep(2.0)

    async def _worker_loop(self, slot: int) -> None:
        while not self.stopping:
            task = await asyncio.to_thread(self.store.lease)
            if task is None:
                await asyncio.sleep(2.0)
                continue
            try:
                record = await self.execute(task)
                await self.settle(task, record.status)
            except Exception:
                LOG.exception("worker slot %d crashed on task %s", slot, task.id)
                self.store.nack(task.id, config.MAX_TASK_ATTEMPTS)

    async def run(self) -> None:
        LOG.info("", extra=event_kwargs(
            "up", "supervisor",
            f"{self.max_concurrency} worker slot(s), {len(self.sensors)} sensor(s)"))
        self.recover()
        coros = [self._sensor_loop(), self._sim_loop(), self._dispatch_loop()]
        coros += [self._worker_loop(i) for i in range(self.max_concurrency)]
        stop_watch = asyncio.create_task(self._watch_stop_flag())
        try:
            await asyncio.gather(*coros)
        finally:
            stop_watch.cancel()
            LOG.info("", extra=event_kwargs("up", "supervisor", "down"))

    async def _watch_stop_flag(self) -> None:
        while not self.stopping:
            if config.STOP_FLAG.exists():
                LOG.info("stop flag seen; draining")
                self.request_stop()
            await asyncio.sleep(1.0)


def build_default_supervisor() -> Supervisor:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.STOP_FLAG.unlink(missing_ok=True)

    store = SqliteStore(config.DB_PATH, ledger_path=config.LEDGER_PATH)
    return Supervisor(
        store=store,
        orchestrator=Orchestrator(store),
        gate=MergeGate(config.REPO_ROOT, config.WORKTREE_ROOT / "integration", config.MAIN_BRANCH),
        worktrees=WorktreeManager(config.REPO_ROOT, config.WORKTREE_ROOT, config.MAIN_BRANCH),
        specs=SPECS,
        repo_root=config.REPO_ROOT,
        max_concurrency=config.MAX_CONCURRENT_WORKERS,
        sensors=[
            (GitSensor(config.REPO_ROOT, store, config.MAIN_BRANCH), config.GIT_POLL_S),
            (TimerSensor(config.REPO_ROOT / "requirements.txt", store), config.TIMER_S),
            (CoverageSensor(config.REPO_ROOT, store, config.COVERAGE_THRESHOLD), config.TIMER_S),
            (AnomalySensor(config.REPO_ROOT / "outcomes.jsonl", store,
                           config.ANOMALY_Z_THRESHOLD, config.ANOMALY_BATCH_ROUNDS),
             config.SIM_RUNNER_S),
        ],
    )
