from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from agents.types import Event

LOG = logging.getLogger("agents.sensors.coverage")


def default_runner(repo_root: Path, python: str = sys.executable) -> dict:
    """Run the suite under coverage and return the parsed JSON report."""
    subprocess.run([python, "-m", "coverage", "run", "-m", "pytest", "-q"],
                   cwd=repo_root, capture_output=True, text=True)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        out_path = Path(fh.name)
    subprocess.run([python, "-m", "coverage", "json", "-o", str(out_path)],
                   cwd=repo_root, capture_output=True, text=True, check=True)
    try:
        return json.loads(out_path.read_text())
    finally:
        out_path.unlink(missing_ok=True)


class CoverageSensor:
    def __init__(self, repo_root: Path, store, threshold: float = 80.0,
                 package: str = "casino", runner=None):
        self.repo_root = Path(repo_root)
        self.store = store
        self.threshold = threshold
        self.package = package
        self.runner = runner or (lambda: default_runner(self.repo_root))

    def poll(self) -> list[Event]:
        try:
            report = self.runner()
        except Exception:
            LOG.exception("coverage run failed; emitting no events this cycle")
            return []

        events: list[Event] = []
        for path, data in sorted(report.get("files", {}).items()):
            if not path.startswith(f"{self.package}/"):
                continue
            pct = float(data.get("summary", {}).get("percent_covered", 0.0))
            if pct >= self.threshold:
                continue
            events.append(Event(
                type="coverage.gap",
                payload={"module": path, "pct": pct, "threshold": self.threshold,
                         "missing": data.get("missing_lines", [])},
                source="coverage_sensor",
            ))
        return events
