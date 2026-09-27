"""Durable persistence for UPSHIFT migration tasks.

:mod:`app.persistence.task_store` is a record of what happened, not an
orchestrator. There is no queue, no worker, and no scheduler; see that module's
docstring for the bounded idempotency rules.
"""

from __future__ import annotations

__all__ = [
    "DuplicateTaskError",
    "InterruptedTaskError",
    "TaskRecord",
    "TaskStore",
    "open_task_store",
]


def __getattr__(name: str):
    """Expose the store lazily, so importing this package costs no database work."""

    if name in __all__:
        from app.persistence import task_store

        return getattr(task_store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
