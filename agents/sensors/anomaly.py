from __future__ import annotations

import json
import logging
import math
from collections import deque
from pathlib import Path

from agents.types import Event

LOG = logging.getLogger("agents.sensors.anomaly")

BASELINE_KEY = "anomaly.baseline"
WINNERS = {"player", "dealer", "push"}
MIN_HAND, MAX_HAND = 4, 30  # two 2s is the floor; a bust cannot exceed 30


def two_proportion_z(x1: int, n1: int, x2: int, n2: int) -> float:
    """Standard two-proportion z. Sample 1 is the observation, sample 2 the baseline."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p_pool = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return ((x1 / n1) - (x2 / n2)) / se


def invariant_violations(rows) -> list[tuple[str, dict]]:
    """Impossibilities, not variance. Deliberately winner-conditional: a bust
    legitimately records a raw value above 21, so `value > 21` alone is normal."""
    out: list[tuple[str, dict]] = []
    for row in rows:
        winner = row.get("winner")
        pv, dv = row.get("player_value"), row.get("dealer_value")

        if winner not in WINNERS:
            out.append(("unknown_winner", row))
            continue
        for label, value in (("player_value", pv), ("dealer_value", dv)):
            if not isinstance(value, int) or not (MIN_HAND <= value <= MAX_HAND):
                out.append((f"{label}_out_of_range", row))
        if not (isinstance(pv, int) and isinstance(dv, int)):
            continue
        if winner == "player" and pv > 21:
            out.append(("player_won_while_bust", row))
        if winner == "dealer" and dv > 21 and pv <= 21:
            out.append(("dealer_won_while_bust", row))
        if winner == "push" and (pv > 21 or dv > 21):
            out.append(("push_with_a_bust", row))
    return out


def read_tail(path: Path, limit: int) -> list[dict]:
    rows: deque[dict] = deque(maxlen=limit)
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a partially written line during an append; skip it
    return list(rows)


class AnomalySensor:
    def __init__(self, outcomes_path: Path, store, z_threshold: float = 3.0, batch: int = 200):
        self.outcomes_path = Path(outcomes_path)
        self.store = store
        self.z_threshold = z_threshold
        self.batch = batch

    def poll(self) -> list[Event]:
        if not self.outcomes_path.exists():
            return []
        rows = read_tail(self.outcomes_path, self.batch)
        if not rows:
            return []

        events: list[Event] = []

        seen: set[str] = set()
        for kind, row in invariant_violations(rows):
            if kind in seen:  # one event per kind per cycle, not per row
                continue
            seen.add(kind)
            events.append(Event(type="outcome.invariant_violation",
                                payload={"kind": kind, "row": row},
                                source="anomaly_sensor"))

        wins = sum(1 for r in rows if r.get("winner") == "player")
        raw = self.store.get_meta(BASELINE_KEY)
        if raw is None:
            self.store.set_meta(BASELINE_KEY, json.dumps({"wins": wins, "n": len(rows)}))
            LOG.info("anomaly baseline recorded: %d/%d player wins", wins, len(rows))
            return events

        baseline = json.loads(raw)
        z = two_proportion_z(wins, len(rows), baseline["wins"], baseline["n"])
        if abs(z) >= self.z_threshold:
            events.append(Event(
                type="outcome.anomaly",
                payload={"z": round(z, 3),
                         "observed_rate": round(wins / len(rows), 4), "observed_n": len(rows),
                         "baseline_rate": round(baseline["wins"] / baseline["n"], 4),
                         "baseline_n": baseline["n"]},
                source="anomaly_sensor",
            ))
        return events
