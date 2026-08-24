from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from agents import config
from agents.adapters.sqlite_store import SqliteStore
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

    # ------------------------------------------------------------------ run one

    async def execute(self, task: Task) -> RunRecord:
        spec = self.specs[task.worker]
        run_id = uuid.uuid4().hex[:12]
        wt = self.worktrees.create(task.worker)

        record = RunRecord(run_id=run_id, worker=spec.name, event_type=task.event.type,
                           task_id=task.id, branch=wt.branch, status="dispatched",
                           started_at=utcnow())
        self.store.record(record)
        LOG.info("[%s] %s starting on %s", run_id, spec.name, wt.branch)

        outcome = await self.agent_runner(spec, task_brief(task.event), wt.path)

        def finish(status: str, files=(), error=None) -> RunRecord:
            final = RunRecord(
                run_id=run_id, worker=spec.name, event_type=task.event.type, task_id=task.id,
                branch=wt.branch, status=status, started_at=record.started_at,
                ended_at=utcnow(), cost_usd=outcome.cost_usd, num_turns=outcome.num_turns,
                files_changed=tuple(files), summary=outcome.summary, error=error,
            )
            self.store.record(final)
            LOG.info("[%s] %s -> %s ($%.4f, %d turns)", run_id, spec.name, status,
                     outcome.cost_usd, outcome.num_turns)
            return final

        if outcome.status != "agent_done":
            self.worktrees.park(wt)
            return finish(outcome.status, error=outcome.error)

        # Cross-worker handoffs are published whether or not the merge succeeds:
        # the investigator's value is the finding, not the file it wrote.
        for event in parse_handoffs(outcome.summary, task.event):
            self.store.publish(event)

        await asyncio.to_thread(ensure_committed, wt.path,
                                f"{spec.name}: automated change ({task.event.type})")

        async with self._gate_lock:
            result = await asyncio.to_thread(self.gate.submit, wt.branch, spec.write_scope)

        if result.status == "merged":
            self.worktrees.cleanup(wt)
            return finish("merged", result.changed_files)

        self.worktrees.park(wt)
        if result.status == "tests_failed":
            self.store.publish(task.event.child(
                "test.failed", {"branch": wt.branch, "detail": result.detail}))
        return finish(result.status, result.changed_files, error=result.detail)

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
                    LOG.info("%s produced %d event(s)", type(sensor).__name__, len(events))
            await asyncio.sleep(1.0)

    async def _sim_loop(self) -> None:
        while not self.stopping:
            await asyncio.to_thread(run_simulation, self.repo_root, config.ANOMALY_BATCH_ROUNDS)
            await asyncio.sleep(config.SIM_RUNNER_S)

    async def _dispatch_loop(self) -> None:
        while not self.stopping:
            try:
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
                if record.status == "merged":
                    self.store.ack(task.id)
                else:
                    LOG.warning("task %s ended %s; nacking", task.id, record.status)
                    self.store.nack(task.id, config.MAX_TASK_ATTEMPTS)
            except Exception:
                LOG.exception("worker slot %d crashed on task %s", slot, task.id)
                self.store.nack(task.id, config.MAX_TASK_ATTEMPTS)

    async def run(self) -> None:
        LOG.info("supervisor up: %d worker slot(s), %d sensor(s)",
                 self.max_concurrency, len(self.sensors))
        coros = [self._sensor_loop(), self._sim_loop(), self._dispatch_loop()]
        coros += [self._worker_loop(i) for i in range(self.max_concurrency)]
        stop_watch = asyncio.create_task(self._watch_stop_flag())
        try:
            await asyncio.gather(*coros)
        finally:
            stop_watch.cancel()
            LOG.info("supervisor down")

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
