import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.sensors.coverage import CoverageSensor

REPORT = {
    "files": {
        "casino/table.py": {"summary": {"percent_covered": 0.0}, "missing_lines": [11, 12, 13]},
        "casino/hand.py": {"summary": {"percent_covered": 95.0}, "missing_lines": []},
        "casino/cards.py": {"summary": {"percent_covered": 42.5}, "missing_lines": [7]},
        "tests/test_hand.py": {"summary": {"percent_covered": 100.0}, "missing_lines": []},
        "agents/worker.py": {"summary": {"percent_covered": 10.0}, "missing_lines": [1]},
    }
}


@pytest.fixture
def sensor(tmp_path):
    return CoverageSensor(repo_root=tmp_path, store=SqliteStore(tmp_path / "t.db"),
                          threshold=80.0, package="casino", runner=lambda: REPORT)


def test_only_modules_below_threshold_are_reported(sensor):
    modules = {e.payload["module"] for e in sensor.poll()}
    assert modules == {"casino/table.py", "casino/cards.py"}


def test_payload_carries_pct_threshold_and_missing_lines(sensor):
    table = next(e for e in sensor.poll() if e.payload["module"] == "casino/table.py")
    assert table.type == "coverage.gap"
    assert table.payload["pct"] == 0.0
    assert table.payload["threshold"] == 80.0
    assert table.payload["missing"] == [11, 12, 13]
    assert table.source == "coverage_sensor"


def test_files_outside_the_package_are_ignored(sensor):
    reported = {e.payload["module"] for e in sensor.poll()}
    assert "agents/worker.py" not in reported
    assert "tests/test_hand.py" not in reported


def test_a_runner_that_fails_yields_no_events_rather_than_raising(tmp_path):
    def boom():
        raise RuntimeError("coverage exploded")

    sensor = CoverageSensor(repo_root=tmp_path, store=SqliteStore(tmp_path / "t.db"),
                            runner=boom)
    assert sensor.poll() == []
