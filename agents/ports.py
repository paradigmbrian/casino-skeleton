from __future__ import annotations

from abc import ABC, abstractmethod

from agents.types import Event, RunRecord, Task


class EventBus(ABC):
    """Local: SQLite table. AWS: EventBridge custom bus."""

    @abstractmethod
    def publish(self, event: Event) -> None: ...

    @abstractmethod
    def drain(self, limit: int = 50) -> list[Event]: ...


class WorkQueue(ABC):
    """Local: SQLite table with a partial unique index. AWS: SQS + DLQ."""

    @abstractmethod
    def enqueue(self, task: Task) -> bool:
        """False when an identical unit of work is already queued or leased."""

    @abstractmethod
    def lease(self) -> Task | None: ...

    @abstractmethod
    def ack(self, task_id: str) -> None: ...

    @abstractmethod
    def nack(self, task_id: str, max_attempts: int) -> str:
        """Returns 'requeued' or 'dead_lettered'."""

    @abstractmethod
    def reclaim_expired_leases(self, timeout_s: int, max_attempts: int) -> list[str]:
        """Redeliver work whose holder died. SQS does this natively via the
        visibility timeout, so the AWS adapter is a no-op."""

    @abstractmethod
    def depth(self) -> dict[str, int]: ...


class RunStore(ABC):
    """Local: SQLite table mirrored to JSONL. AWS: DynamoDB + CloudWatch Logs."""

    @abstractmethod
    def record(self, run: RunRecord) -> None: ...

    @abstractmethod
    def recent(self, limit: int = 20) -> list[RunRecord]: ...

    @abstractmethod
    def cost_since(self, iso_timestamp: str) -> float: ...

    @abstractmethod
    def close_dangling_runs(self, status: str = "interrupted") -> list[str]:
        """Runs still open when the process died. Left alone they sit at
        'dispatched' forever and the ledger stops being reconstructable."""

    @abstractmethod
    def get_meta(self, key: str) -> str | None: ...

    @abstractmethod
    def set_meta(self, key: str, value: str) -> None: ...
