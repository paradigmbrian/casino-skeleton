import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.sensors.timer import TimerSensor, parse_requirements


def test_parse_requirements_reads_exact_pins():
    text = "requests==2.6.0\n# a comment\n\nflask==3.0.0\n"
    assert parse_requirements(text) == {"requests": "2.6.0", "flask": "3.0.0"}


def test_parse_requirements_ignores_ranges_and_blank_lines():
    assert parse_requirements("requests>=2.0\n\n  \nurllib3~=2.0\n") == {}


def test_parse_requirements_strips_inline_comments_and_whitespace():
    assert parse_requirements("  requests==2.6.0  # old\n") == {"requests": "2.6.0"}


@pytest.fixture
def reqs(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("requests==2.6.0\n")
    return path


def test_a_stale_pin_emits_one_event(reqs, tmp_path):
    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=lambda p: "2.32.3")
    (event,) = sensor.poll()
    assert event.type == "deps.stale"
    assert event.payload == {"package": "requests", "pinned": "2.6.0", "latest": "2.32.3"}
    assert event.source == "timer_sensor"


def test_a_current_pin_emits_nothing(reqs, tmp_path):
    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=lambda p: "2.6.0")
    assert sensor.poll() == []


def test_the_same_upgrade_is_announced_once_per_target_version(reqs, tmp_path):
    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=lambda p: "2.32.3")
    assert len(sensor.poll()) == 1
    assert sensor.poll() == []            # already announced
    sensor.fetcher = lambda p: "2.33.0"   # a newer release is new news
    assert len(sensor.poll()) == 1


def test_a_fetch_failure_is_skipped_quietly(reqs, tmp_path):
    def boom(package):
        raise OSError("no network")

    sensor = TimerSensor(reqs, SqliteStore(tmp_path / "t.db"), fetcher=boom)
    assert sensor.poll() == []


def test_a_missing_requirements_file_yields_nothing(tmp_path):
    sensor = TimerSensor(tmp_path / "nope.txt", SqliteStore(tmp_path / "t.db"),
                         fetcher=lambda p: "1.0.0")
    assert sensor.poll() == []
