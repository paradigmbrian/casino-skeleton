import json

import pytest

from agents.adapters.sqlite_store import SqliteStore
from agents.sensors.anomaly import (AnomalySensor, invariant_violations, read_tail,
                                    two_proportion_z)


def row(winner="player", pv=20, dv=18):
    return {"winner": winner, "player_strategy": "basic_17", "dealer_strategy": "standard_17",
            "player_value": pv, "dealer_value": dv}


def write_rows(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# --- statistics -------------------------------------------------------------

def test_identical_proportions_give_a_zero_z():
    assert two_proportion_z(42, 100, 420, 1000) == pytest.approx(0.0, abs=1e-9)


def test_a_large_shift_gives_a_large_z():
    assert two_proportion_z(90, 100, 420, 1000) > 3.0


def test_z_is_signed_by_direction():
    assert two_proportion_z(10, 100, 420, 1000) < -3.0


def test_degenerate_samples_do_not_divide_by_zero():
    assert two_proportion_z(0, 10, 0, 10) == 0.0


# --- invariants -------------------------------------------------------------

def test_a_bust_recorded_as_a_dealer_win_is_not_a_violation():
    """Busts legitimately log raw values above 21 -- the seed data contains
    dealer_value 25. Only a *winner-conditional* impossibility counts."""
    assert invariant_violations([row(winner="dealer", pv=25, dv=19)]) == []


def test_a_player_winning_while_bust_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(winner="player", pv=23, dv=18)])]
    assert "player_won_while_bust" in kinds


def test_a_dealer_winning_while_bust_against_a_live_player_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(winner="dealer", pv=19, dv=24)])]
    assert "dealer_won_while_bust" in kinds


def test_an_unknown_winner_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(winner="house")])]
    assert "unknown_winner" in kinds


def test_an_impossible_hand_value_is_a_violation():
    kinds = [k for k, _ in invariant_violations([row(pv=3)])]
    assert "player_value_out_of_range" in kinds


def test_ordinary_rows_produce_no_violations():
    assert invariant_violations([row(), row("dealer", 18, 20), row("push", 19, 19)]) == []


# --- sensor -----------------------------------------------------------------

def test_read_tail_returns_the_last_n_rows_and_skips_bad_lines(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join([json.dumps(row(pv=i)) for i in (10, 11, 12)] + ["{not json"]) + "\n")
    assert [r["player_value"] for r in read_tail(path, 2)] == [11, 12]


def test_the_first_poll_records_a_baseline_and_emits_no_drift(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    write_rows(path, [row("player")] * 40 + [row("dealer")] * 60)
    store = SqliteStore(tmp_path / "t.db")
    sensor = AnomalySensor(path, store, batch=100)
    assert [e for e in sensor.poll() if e.type == "outcome.anomaly"] == []
    assert store.get_meta("anomaly.baseline") is not None


def test_a_large_drift_from_the_baseline_emits_an_anomaly(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    store = SqliteStore(tmp_path / "t.db")
    write_rows(path, [row("player")] * 40 + [row("dealer")] * 60)
    sensor = AnomalySensor(path, store, batch=100)
    sensor.poll()                                        # establishes the baseline
    write_rows(path, [row("player")] * 90 + [row("dealer")] * 10)
    (event,) = [e for e in sensor.poll() if e.type == "outcome.anomaly"]
    assert abs(event.payload["z"]) > 3.0
    assert event.payload["baseline_n"] == 100
    assert event.source == "anomaly_sensor"


def test_a_batch_matching_the_baseline_emits_nothing(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    store = SqliteStore(tmp_path / "t.db")
    write_rows(path, [row("player")] * 40 + [row("dealer")] * 60)
    sensor = AnomalySensor(path, store, batch=100)
    sensor.poll()
    write_rows(path, [row("player")] * 41 + [row("dealer")] * 59)
    assert [e for e in sensor.poll() if e.type == "outcome.anomaly"] == []


def test_invariant_violations_are_emitted_even_before_a_baseline_exists(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    write_rows(path, [row(winner="player", pv=25)] * 3)
    sensor = AnomalySensor(path, SqliteStore(tmp_path / "t.db"), batch=100)
    kinds = [e.payload["kind"] for e in sensor.poll() if e.type == "outcome.invariant_violation"]
    assert kinds and all(k == "player_won_while_bust" for k in kinds)


def test_a_missing_outcomes_file_yields_nothing(tmp_path):
    sensor = AnomalySensor(tmp_path / "nope.jsonl", SqliteStore(tmp_path / "t.db"))
    assert sensor.poll() == []
