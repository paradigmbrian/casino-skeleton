from __future__ import annotations

from pathlib import PurePosixPath


def _normalise(path: str) -> str | None:
    """Repo-relative posix path, or None if the path escapes the repo."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute():
        return None
    parts: list[str] = []
    for part in p.parts:
        if part in (".", ""):
            continue
        if part == "..":
            return None  # never allow traversal, even if it would resolve inside
        parts.append(part)
    return "/".join(parts) if parts else None


def in_scope(path: str, scope: tuple[str, ...]) -> bool:
    norm = _normalise(path)
    if norm is None:
        return False
    for entry in scope:
        if entry.endswith("/"):
            if norm.startswith(entry):
                return True
        elif norm == entry:
            return True
    return False


def out_of_scope(paths, scope: tuple[str, ...]) -> list[str]:
    return [p for p in paths if not in_scope(p, scope)]
