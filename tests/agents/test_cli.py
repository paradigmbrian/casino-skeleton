from agents.adapters.sqlite_store import SqliteStore
from agents.cli import format_status
from agents.types import Event, RunRecord, Task


def test_status_reports_queue_depth_runs_and_spend(tmp_path):
    store = SqliteStore(tmp_path / "t.db", ledger_path=tmp_path / "runs.jsonl")
    ev = Event(type="coverage.gap", payload={"module": "casino/table.py"})
    store.enqueue(Task(worker="test-author", event=ev, dedupe_key=ev.dedupe_key()))
    store.record(RunRecord(run_id="r1", worker="reviewer", event_type="commit.pushed",
                           task_id="t1", branch="agent/review-x", status="merged",
                           started_at="2026-08-23T12:00:00+00:00", cost_usd=0.25,
                           files_changed=("casino/hand.py",)))
    out = format_status(store)
    assert "queued=1" in out
    assert "reviewer" in out
    assert "merged" in out
    assert "0.25" in out


def test_status_is_readable_when_nothing_has_happened(tmp_path):
    out = format_status(SqliteStore(tmp_path / "t.db"))
    assert "queued=0" in out
    assert "no runs yet" in out.lower()


def test_verbose_is_accepted_before_and_after_the_subcommand():
    """argparse binds a top-level flag strictly before the subcommand, so -v is
    declared on both -- `agents up -v` is how it actually gets typed."""
    import argparse
    import pytest
    from agents.cli import build_parser

    for argv in (["-v", "status"], ["status", "-v"], ["events", "-v", "--limit", "5"]):
        assert build_parser().parse_args(argv).verbose is True
    assert build_parser().parse_args(["status"]).verbose is False


def test_verbose_does_not_turn_on_the_sdk_firehose():
    """-v used to set the *root* logger to DEBUG, which buried four useful lines
    per run under SDK transport and asyncio chatter."""
    import logging
    from agents.cli import configure_logging

    configure_logging(verbose=True, debug_sdk=False, log_format="text", stream=None)
    assert logging.getLogger("agents").level == logging.DEBUG
    for noisy in ("claude_agent_sdk", "asyncio", "httpx"):
        assert logging.getLogger(noisy).level >= logging.WARNING


def test_debug_sdk_opts_into_the_firehose():
    import logging
    from agents.cli import configure_logging

    configure_logging(verbose=True, debug_sdk=True, log_format="text", stream=None)
    assert logging.getLogger("claude_agent_sdk").level == logging.DEBUG


def test_json_format_is_still_available_for_machines():
    import io
    import json
    import logging
    from agents.cli import configure_logging
    from agents.logfmt import event_kwargs

    stream = io.StringIO()
    configure_logging(verbose=False, debug_sdk=False, log_format="json", stream=stream)
    logging.getLogger("agents.test").info("ignored", extra=event_kwargs("merged", "reviewer", "x"))
    payload = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert payload["kind"] == "merged"
    assert payload["actor"] == "reviewer"


def test_default_level_keeps_our_own_logger_at_info():
    import logging
    from agents.cli import configure_logging

    configure_logging(verbose=False, debug_sdk=False, log_format="text", stream=None)
    assert logging.getLogger("agents").level == logging.INFO
