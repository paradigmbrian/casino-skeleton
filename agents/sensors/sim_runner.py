from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

LOG = logging.getLogger("agents.sensors.sim_runner")


def run_simulation(repo_root: Path, rounds: int, python: str = sys.executable) -> bool:
    """Keep fresh outcomes arriving so the anomaly sensor has something to read.
    Never raises -- a broken simulator is itself a signal, not a supervisor crash."""
    proc = subprocess.run(
        [python, "-c", f"from casino.simulate import run; run({int(rounds)})"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        LOG.warning("simulation failed (%d): %s", proc.returncode, proc.stderr.strip()[-400:])
        return False
    return True
