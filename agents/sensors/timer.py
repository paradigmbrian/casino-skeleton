from __future__ import annotations

import json
import logging
import re
import urllib.request
from pathlib import Path

from agents.types import Event

LOG = logging.getLogger("agents.sensors.timer")

PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-+!]+)\s*$")
META_PREFIX = "deps.announced."


def parse_requirements(text: str) -> dict[str, str]:
    """Exact pins only. A range is a deliberate choice by a human; leave it alone."""
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        match = PIN_RE.match(line)
        if match:
            pins[match.group(1)] = match.group(2)
    return pins


def fetch_latest(package: str, timeout: float = 10.0) -> str | None:
    url = f"https://pypi.org/pypi/{package}/json"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)["info"]["version"]


class TimerSensor:
    def __init__(self, requirements_path: Path, store, fetcher=None):
        self.requirements_path = Path(requirements_path)
        self.store = store
        self.fetcher = fetcher or fetch_latest

    def poll(self) -> list[Event]:
        if not self.requirements_path.exists():
            return []

        events: list[Event] = []
        for package, pinned in parse_requirements(self.requirements_path.read_text()).items():
            try:
                latest = self.fetcher(package)
            except Exception as exc:
                LOG.warning("PyPI lookup for %s failed: %s", package, exc)
                continue
            if not latest or latest == pinned:
                continue

            # Announce a given upgrade once, not every 90 seconds.
            key = f"{META_PREFIX}{package}"
            if self.store.get_meta(key) == latest:
                continue
            self.store.set_meta(key, latest)

            events.append(Event(
                type="deps.stale",
                payload={"package": package, "pinned": pinned, "latest": latest},
                source="timer_sensor",
            ))
        return events
