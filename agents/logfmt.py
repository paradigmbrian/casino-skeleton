from __future__ import annotations

import logging
import os
import sys

# symbol, colour, and whether the kind is loud enough to shout
KINDS: dict[str, tuple[str, str, bool]] = {
    "dispatch": ("▸", "36", False),   # cyan   -- routed to a worker
    "start":    ("●", "34", False),   # blue   -- a run began
    "sensor":   ("·", "90", False),   # grey   -- something was observed
    "merged":   ("✓", "32", True),    # green  -- it landed on main
    "salvaged": ("✓", "32", False),   # green  -- committed work rescued
    "rejected": ("✗", "33", True),    # yellow -- gate said no
    "budget":   ("✗", "33", True),    # yellow -- hit a ceiling
    "failed":   ("✗", "31", True),    # red    -- run errored
    "recover":  ("⟲", "35", False),   # purple -- cleaning up after a crash
    "up":       ("▶", "1",  False),   # bold   -- supervisor lifecycle
}
DEFAULT = ("·", "", False)

KIND_W, ACTOR_W = 9, 22


def event_kwargs(kind: str, actor: str, detail: str = "") -> dict[str, str]:
    """The `extra=` payload for a structured log line. Call sites pass fields
    rather than pre-formatted prose, so the formatter owns the layout and a
    machine-readable formatter can use the same records."""
    return {"kind": kind, "actor": actor, "detail": detail}


def should_colour(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stderr
    return bool(getattr(stream, "isatty", lambda: False)())


class EventFormatter(logging.Formatter):
    """One aligned line per event. Records without a `kind` -- anything a
    library logged -- fall through to plain prose so nothing is hidden."""

    def __init__(self, colour: bool = True):
        super().__init__(datefmt="%H:%M:%S")
        self.colour = colour

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour and code else text

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, self.datefmt)
        kind = getattr(record, "kind", None)

        if kind is None:
            body = record.getMessage()
            if record.exc_info:
                body += "\n" + self.formatException(record.exc_info)
            if record.levelno >= logging.WARNING:
                body = self._paint(body, "31" if record.levelno >= logging.ERROR else "33")
            return f"{ts}  {'':1} {'':{KIND_W}}{body}"

        symbol, colour, loud = KINDS.get(kind, DEFAULT)
        label = kind.upper() if loud else kind
        actor = str(getattr(record, "actor", ""))
        detail = str(getattr(record, "detail", "") or record.getMessage())

        line = (f"{ts}  {self._paint(symbol, colour)} "
                f"{self._paint(label.ljust(KIND_W), colour if loud else '')}"
                f"{actor.ljust(ACTOR_W)}{detail}")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line.rstrip()
