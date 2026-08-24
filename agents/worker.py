from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    query,
)
from claude_agent_sdk._errors import ResultError

from agents.scope import in_scope

LOG = logging.getLogger("agents.worker")

# Tools that mutate the filesystem. Everything else is read-only or inert.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    triggers: tuple[str, ...]
    system_prompt: str
    allowed_tools: tuple[str, ...]
    write_scope: tuple[str, ...]
    max_turns: int = 25
    timeout_s: int = 300
    max_cost_usd: float = 0.50


@dataclass(frozen=True)
class AgentOutcome:
    status: str  # agent_done|timeout|error
    summary: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    error: str | None = None


def make_scope_guard(write_scope: tuple[str, ...], worktree_path: Path | str):
    """Prevention, not detection. A write outside the worker's scope never
    reaches the filesystem -- the merge gate's identical check is the backstop
    for anything that arrives by another route (e.g. a Bash redirect)."""
    root = Path(worktree_path).resolve()

    async def guard(tool_name: str, tool_input: dict[str, Any], context):
        if tool_name not in WRITE_TOOLS:
            return PermissionResultAllow()

        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not raw:
            return PermissionResultDeny(message=f"{tool_name} called without a file path")

        try:
            rel = Path(raw).resolve().relative_to(root)
        except ValueError:
            return PermissionResultDeny(
                message=f"{raw} is outside this worker's worktree ({root}); refused"
            )

        if in_scope(rel.as_posix(), write_scope):
            return PermissionResultAllow()

        return PermissionResultDeny(
            message=(
                f"{rel.as_posix()} is outside this worker's write scope {write_scope}. "
                "Another agent owns that path. Do not attempt it again -- if the work "
                "needs to happen there, say so in your final message instead."
            )
        )

    return guard


# A run that ends at a limit is terminal, not transient: it spent its cap or its
# turns reaching the same wall, and a retry spends them again to reach it twice.
TERMINAL_REASONS = {"budget_exhausted": "budget_exhausted", "max_turns": "max_turns"}


def outcome_from_result_error(exc: ResultError, text: str) -> AgentOutcome:
    """Map a terminal CLI result onto an outcome that tells the truth about cost.

    The SDK raises this *instead of* yielding a ResultMessage, so a naive
    `except Exception` records $0.00 for a run that spent its entire budget --
    and the hourly ceiling is computed by summing those numbers. Undercounting
    there disables the one rail that bounds spend. The raw result payload hangs
    off `.data`, so the real figures are available; use them."""
    data = exc.data or {}
    status = TERMINAL_REASONS.get(exc.terminal_reason or "", "error")
    if status == "error" and exc.subtype == "error_max_budget_usd":
        status = "budget_exhausted"  # belt and braces: subtype without terminal_reason
    detail = {"budget_exhausted": "run stopped at its per-run budget ceiling",
              "max_turns": "run stopped at its turn ceiling"}.get(status, repr(exc))
    return AgentOutcome(
        status=status,
        summary=text[-2000:],
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        num_turns=int(data.get("num_turns") or 0),
        error=detail,
    )


def ensure_committed(worktree_path: Path, message: str) -> bool:
    """Commit anything the agent left uncommitted. Returns True if it committed."""
    status = subprocess.run(["git", "-C", str(worktree_path), "status", "--porcelain"],
                            capture_output=True, text=True).stdout.strip()
    if not status:
        return False
    subprocess.run(["git", "-C", str(worktree_path), "add", "-A"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(worktree_path), "commit", "-m", message], check=True,
                   capture_output=True, text=True)
    return True


def build_options(spec: WorkerSpec, worktree_path: Path,
                  model: str = "claude-opus-5", effort: str = "high") -> ClaudeAgentOptions:
    """Options for one worker run.

    Write tools are deliberately *excluded* from `allowed_tools`. An entry there
    that allows a whole tool auto-approves it before `can_use_tool` is consulted,
    which would shadow the scope guard entirely -- verified against the live SDK,
    which warns about exactly this. Leaving them out makes every write fall
    through to the guard, which then allows or denies it per path."""
    gated = [t for t in spec.allowed_tools if t not in WRITE_TOOLS]
    return ClaudeAgentOptions(
        cwd=str(worktree_path),
        system_prompt=spec.system_prompt,
        allowed_tools=gated,
        # `default` keeps can_use_tool as the sole permission authority. Nothing
        # can hang waiting for a human because the callback answers every request.
        permission_mode="default",
        can_use_tool=make_scope_guard(spec.write_scope, worktree_path),
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_cost_usd,
        model=model,
        effort=effort,
        thinking={"type": "adaptive"},
        setting_sources=None,  # do not inherit the repo's CLAUDE.md or user settings
        stderr=lambda line: LOG.debug("claude-cli: %s", line.rstrip()),
    )


async def run_agent(spec: WorkerSpec, prompt: str, worktree_path: Path,
                    model: str = "claude-opus-5", effort: str = "high") -> AgentOutcome:
    options = build_options(spec, worktree_path, model, effort)

    texts: list[str] = []
    result: ResultMessage | None = None

    async def drive() -> None:
        nonlocal result
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                result = msg

    try:
        await asyncio.wait_for(drive(), timeout=spec.timeout_s)
    except asyncio.TimeoutError:
        return AgentOutcome("timeout", "\n".join(texts)[-2000:],
                            error=f"exceeded {spec.timeout_s}s")
    except ResultError as exc:  # a limit was hit; the cost is real and must be recorded
        LOG.warning("worker %s ended early: %s", spec.name, exc.terminal_reason or exc.subtype)
        return outcome_from_result_error(exc, "\n".join(texts))
    except Exception as exc:  # SDK/CLI failures must not kill the supervisor
        LOG.exception("worker %s crashed", spec.name)
        return AgentOutcome("error", "\n".join(texts)[-2000:], error=repr(exc))

    if result is None:
        return AgentOutcome("error", "\n".join(texts)[-2000:],
                            error="agent produced no ResultMessage")

    return AgentOutcome(
        status="error" if result.is_error else "agent_done",
        summary=(result.result or "\n".join(texts))[-2000:],
        cost_usd=result.total_cost_usd or 0.0,
        num_turns=result.num_turns or 0,
        error=(result.result or "agent reported an error") if result.is_error else None,
    )
