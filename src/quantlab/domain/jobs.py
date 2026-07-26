from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class JobCancelled(RuntimeError):
    pass


class JobBudgetExceeded(RuntimeError):
    pass


__all__ = ["JobBudgetExceeded", "JobCancelled", "JobStatus"]
