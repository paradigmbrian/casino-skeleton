import asyncio

import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.merge_gate import GateResult
from agents.orchestrator import Orchestrator
from agents.specs import SPECS
from agents.supervisor import Supervisor
from agents.types import Event, Task
from agents.worker import AgentOutcome
from agents.worktree import WorktreeManager


class FakeGate:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def submit(self, branch, write_scope):
        self.calls.append((branch, write_scope))
        return self.result


def build(tmp_path, temp_repo, gate, outcome):
    store = SqliteStore(tmp_path / "t.db", ledger_path=tmp_path / "runs.jsonl")
    worktrees = WorktreeManager(temp_repo, tmp_path / "wt", "main")

    async def fake_runner(spec, prompt, worktree_path):
        (worktree_path / "tests").mkdir(exist_ok=True)
        (worktree_path / "tests" / "test_agent.py").write_text("def test_x():\n    assert True\n")
        return outcome

    sup = Supervisor(store=store, orchestrator=Orchestrator(store), gate=gate,
                     worktrees=worktrees, specs=SPECS, repo_root=temp_repo,
                     agent_runner=fake_runner)
    return store, sup


def a_task(worker="test-author", event_type="coverage.gap"):
    ev = Event(type=event_type, payload={"module": "casino/table.py"})
    return Task(worker=worker, event=ev, dedupe_key=ev.dedupe_key())


OK = AgentOutcome("agent_done", summary="wrote tests", cost_usd=0.12, num_turns=6)


def test_a_merged_run_is_recorded_with_cost_and_files(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", ("tests/test_agent.py",), sha="deadbeef"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    rec = asyncio.run(sup.execute(a_task()))
    assert rec.status == "merged"
    assert rec.cost_usd == pytest.approx(0.12)
    assert rec.num_turns == 6
    assert rec.files_changed == ("tests/test_agent.py",)
    assert store.recent()[0].status == "merged"


def test_the_gate_is_called_with_the_workers_own_write_scope(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    asyncio.run(sup.execute(a_task()))
    assert gate.calls[0][1] == SPECS["test-author"].write_scope


def test_a_scope_rejection_is_recorded_and_the_branch_is_parked(tmp_path, temp_repo):
    gate = FakeGate(GateResult("scope_rejected", ("casino/x.py",), detail="outside scope"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    rec = asyncio.run(sup.execute(a_task()))
    assert rec.status == "scope_rejected"
    assert "outside scope" in (rec.error or "")


def test_failing_tests_emit_a_test_failed_event_for_the_test_author(tmp_path, temp_repo):
    gate = FakeGate(GateResult("tests_failed", ("tests/t.py",), detail="1 failed"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    asyncio.run(sup.execute(a_task()))
    types = [e.type for e in store.drain(limit=50)]
    assert "test.failed" in types


def test_a_merged_run_emits_nothing_itself_and_leaves_the_hook_to_the_git_sensor(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", ("tests/t.py",), sha="abc"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    asyncio.run(sup.execute(a_task()))
    assert [e.type for e in store.drain(limit=50)] == []


def test_investigator_handoffs_become_events_for_other_workers(tmp_path, temp_repo):
    outcome = AgentOutcome("agent_done", cost_usd=0.2, num_turns=4, summary=(
        "Done.\nHANDOFF: fix=Use Hand.is_blackjack when resolving a natural.\n"
        "HANDOFF: test=Assert a natural beats a drawn 21.\n"))
    gate = FakeGate(GateResult("merged", ("docs/investigations/x.md",), sha="abc"))
    store, sup = build(tmp_path, temp_repo, gate, outcome)
    asyncio.run(sup.execute(a_task(worker="anomaly-investigator", event_type="outcome.anomaly")))
    types = {e.type for e in store.drain(limit=50)}
    assert types == {"review.fix_requested", "regression.needed"}


def test_a_timed_out_agent_is_recorded_and_does_not_reach_the_gate(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, AgentOutcome("timeout", error="exceeded 300s"))
    rec = asyncio.run(sup.execute(a_task()))
    assert rec.status == "timeout"
    assert gate.calls == []


def test_request_stop_sets_the_flag_the_loop_checks(tmp_path, temp_repo):
    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    assert sup.stopping is False
    sup.request_stop()
    assert sup.stopping is True


def test_recover_requeues_leases_abandoned_by_a_killed_supervisor(tmp_path, temp_repo):
    """Ctrl-C mid-run leaves the task leased and the run record stuck at
    'dispatched'. Nothing else reclaims either -- at startup no run can be in
    flight, so every leased task is by definition abandoned."""
    gate = FakeGate(GateResult("merged", (), sha="abc"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    task = a_task()
    store.enqueue(task)
    store.lease()
    assert store.depth()["leased"] == 1

    sup.recover()

    assert store.depth()["leased"] == 0
    assert store.depth()["queued"] == 1
    assert store.lease().id == task.id


def test_recover_parks_worktrees_left_behind_keeping_their_branches(tmp_path, temp_repo):
    from tests.agents.conftest import git

    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    orphan = sup.worktrees.create("reviewer")
    (orphan.path / "casino" / "hand.py").write_text("VALUE = 22\n")
    git(orphan.path, "commit", "-qam", "review: work in progress")
    assert orphan.path.is_dir()

    sup.recover()

    assert not orphan.path.exists()
    assert orphan.branch in git(temp_repo, "branch", "--list", orphan.branch)


def test_recover_discards_a_worktree_whose_agent_never_committed(tmp_path, temp_repo):
    from tests.agents.conftest import git

    gate = FakeGate(GateResult("merged", (), sha="abc"))
    _, sup = build(tmp_path, temp_repo, gate, OK)
    orphan = sup.worktrees.create("reviewer")

    sup.recover()

    assert not orphan.path.exists()
    assert git(temp_repo, "branch", "--list", orphan.branch) == ""


def test_recover_marks_dangling_run_records_interrupted(tmp_path, temp_repo):
    from agents.types import RunRecord, utcnow

    gate = FakeGate(GateResult("merged", (), sha="abc"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    store.record(RunRecord(run_id="stuck", worker="reviewer", event_type="commit.pushed",
                           task_id="t1", branch="agent/reviewer-x", status="dispatched",
                           started_at=utcnow()))

    sup.recover()

    (rec,) = [r for r in store.recent() if r.run_id == "stuck"]
    assert rec.status == "interrupted"
    assert rec.ended_at is not None


def test_recover_leaves_finished_runs_alone(tmp_path, temp_repo):
    from agents.types import RunRecord, utcnow

    gate = FakeGate(GateResult("merged", (), sha="abc"))
    store, sup = build(tmp_path, temp_repo, gate, OK)
    store.record(RunRecord(run_id="done", worker="reviewer", event_type="commit.pushed",
                           task_id="t1", branch="b", status="merged",
                           started_at=utcnow(), ended_at=utcnow()))
    sup.recover()
    assert [r for r in store.recent() if r.run_id == "done"][0].status == "merged"
