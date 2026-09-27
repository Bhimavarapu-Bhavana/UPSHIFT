"""Durable task records in SQLite, with bounded idempotency.

This is a record of what happened, not an orchestrator. There is no queue, no
worker, no retry loop, no scheduler, and no background thread. A caller runs a
task, writes down what happened, and the record survives a restart.

What is persisted
-----------------
Exactly the fields needed to answer "what did this task do, and is it safe to
run it again":

* ``task_id``, the caller's identifier, which is the primary key;
* ``repository_name``, a name rather than a path, so the record is not a map of
  where code lives on one machine;
* ``request`` and ``plan``, as JSON, so a restart can show what was asked for;
* ``execution``, ``verification``, ``decision``, ``recovery_state``, and
  ``state_history``, as the evidence the run produced;
* ``created_at``, ``updated_at``, and ``finished_at``.

File *content* is never stored. A plan's ``old_content`` and ``new_content`` are
reduced to their lengths when written, so the database cannot become a copy of a
user's source.

Bounded idempotency
-------------------
The rule is a single decision per terminal state, and it is deliberately not a
queue:

* A task that has already reached a **terminal** state is never executed again.
  ``begin`` returns the stored record instead, so a repeated request is answered
  from the record rather than by re-running the migration.
* A task found in **EXECUTING** was interrupted, and its repository may be
  half-migrated. ``begin`` refuses it, so execution fails closed and a human
  decides what to do. There is no automatic resume and no automatic rollback
  from here: guessing at a half-written repository is exactly the thing this
  project refuses to do.
* A task in **PENDING** wrote nothing, so re-running it cannot duplicate any
  effect and the record is replaced.

``finish`` and ``fail`` write a terminal state at most once. A second call
returns the record already stored, so a caller that crashes between writing and
acknowledging cannot produce two outcomes for one task.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.execution.task_state import (
    COMPLETED,
    EXECUTING,
    FAILED,
    PENDING,
    ROLLED_BACK,
    TaskJournal,
)

__all__ = [
    "DuplicateTaskError",
    "InterruptedTaskError",
    "TERMINAL_STATES",
    "TaskRecord",
    "TaskStore",
    "open_task_store",
]

#: A task in one of these states has an outcome and will not run again.
TERMINAL_STATES = frozenset({COMPLETED, ROLLED_BACK, FAILED})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    repository_name   TEXT NOT NULL,
    state             TEXT NOT NULL,
    request           TEXT NOT NULL,
    plan              TEXT,
    execution         TEXT,
    verification      TEXT,
    decision          TEXT,
    recovery_state    TEXT,
    state_history     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS tasks_by_state ON tasks (state);
"""


class DuplicateTaskError(RuntimeError):
    """Raised when a task id is reused for a run that already has an outcome."""


class InterruptedTaskError(RuntimeError):
    """Raised when a task was interrupted mid-execution.

    Its repository may be half-migrated, so it fails closed: a caller must
    inspect the state and decide, and no automatic retry is offered.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact_plan(plan: Any) -> Optional[List[Dict[str, Any]]]:
    """Reduce a plan to lengths, so the database never stores file content."""

    if plan is None:
        return None
    operations = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        operations.append(
            {
                "operation": entry.get("operation"),
                "path": entry.get("path"),
                "expected_occurrences": entry.get("expected_occurrences", 1),
                "old_content_length": len(entry.get("old_content", "") or ""),
                "new_content_length": len(entry.get("new_content", "") or ""),
            }
        )
    return operations


def _redact_request(request: Any) -> Dict[str, Any]:
    """Keep the request's meaning, drop anything that could carry content."""

    if not isinstance(request, dict):
        return {"summary": str(request)}
    return {
        "migration_name": request.get("migration_name"),
        "dry_run": bool(request.get("dry_run", True)),
        "observed_symbols": list(request.get("observed_symbols", ())),
        "operation_count": len(request.get("plan", ()) or ()),
    }


@dataclass(frozen=True)
class TaskRecord:
    """One stored task. A plain frozen value with no database handle."""

    task_id: str
    repository_name: str
    state: str
    request: Dict[str, Any]
    plan: Optional[Tuple[Dict[str, Any], ...]]
    execution: Optional[Dict[str, Any]]
    verification: Optional[Dict[str, Any]]
    decision: Optional[str]
    recovery_state: Optional[str]
    state_history: Tuple[str, ...]
    created_at: str
    updated_at: str
    finished_at: Optional[str]

    @property
    def is_terminal(self) -> bool:
        """True when the task has an outcome and will not run again."""

        return self.state in TERMINAL_STATES

    @property
    def was_interrupted(self) -> bool:
        """True when the task was recorded mid-execution and never finished."""

        return self.state == EXECUTING

    @property
    def migration_accepted(self) -> bool:
        """True only when the stored decision accepted the migration.

        Read from the decision, never from the terminal state, so a rolled-back
        task reads as unaccepted.
        """

        return self.decision == "ACCEPT"

    def replay_journal(self) -> TaskJournal:
        """Rebuild the journal from the stored history.

        States are read back rather than recomputed, so a restart cannot invent
        progress or lose it.
        """

        steps = self.request.get("steps", ())
        events = []
        for index, state in enumerate(self.state_history):
            step = steps[index] if index < len(steps) else state
            events.append({"state": state, "step": step, "succeeded": True, "detail": ""})
        return TaskJournal.replay(self.task_id, events)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable projection."""

        return {
            "task_id": self.task_id,
            "repository_name": self.repository_name,
            "state": self.state,
            "request": self.request,
            "plan": [dict(entry) for entry in self.plan] if self.plan else None,
            "execution": self.execution,
            "verification": self.verification,
            "decision": self.decision,
            "recovery_state": self.recovery_state,
            "state_history": list(self.state_history),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "is_terminal": self.is_terminal,
            "was_interrupted": self.was_interrupted,
            "migration_accepted": self.migration_accepted,
        }


class TaskStore:
    """A SQLite-backed record of tasks.

    Args:
        database_path: The SQLite file. Its parent directory must exist.
    """

    def __init__(self, database_path: Any) -> None:
        self._path = Path(database_path)
        if not self._path.parent.is_dir():
            raise ValueError("the task store's parent directory must exist")
        self._connection = sqlite3.connect(str(self._path))
        self._connection.row_factory = sqlite3.Row
        # ON DELETE is never used: a task record is an audit trail, so a task is
        # finished or failed, not deleted.
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        """Close the connection."""

        self._connection.close()

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writing -----------------------------------------------------------

    def begin(
        self,
        task_id: str,
        *,
        repository_name: str,
        request: Any,
        plan: Optional[Sequence[Any]] = None,
    ) -> Tuple[TaskRecord, bool]:
        """Record that a task is about to run, or refuse to run it again.

        Returns:
            ``(record, created)``. ``created`` is False when an existing
            terminal record was returned unchanged, which is how a repeated
            request is answered without re-executing anything.

        Raises:
            ValueError: If ``task_id`` is not non-empty text.
            InterruptedTaskError: If the task was recorded mid-execution, so its
                repository may be half-migrated. This fails closed.
        """

        identifier = _text(task_id, "task_id")
        existing = self.get(identifier)
        if existing is not None:
            if existing.is_terminal:
                return existing, False
            if existing.state == EXECUTING:
                raise InterruptedTaskError(
                    f"task {identifier!r} was interrupted while executing; its "
                    "repository may be half-migrated, so it will not be run "
                    "again automatically"
                )
            # PENDING wrote nothing, so replacing it cannot duplicate an effect.
        now = _now()
        self._connection.execute(
            "INSERT OR REPLACE INTO tasks "
            "(task_id, repository_name, state, request, plan, execution, "
            " verification, decision, recovery_state, state_history, "
            " created_at, updated_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL)",
            (
                identifier,
                _text(repository_name, "repository_name"),
                PENDING,
                json.dumps(_redact_request(request), sort_keys=True),
                json.dumps(_redact_plan(plan), sort_keys=True),
                json.dumps([PENDING]),
                now,
                now,
            ),
        )
        self._connection.commit()
        record = self.get(identifier)
        assert record is not None
        return record, True

    def mark_executing(self, task_id: str) -> TaskRecord:
        """Record that writes have begun, so an interruption is detectable."""

        return self._update(task_id, {"state": EXECUTING}, terminal=False)

    def finish(
        self,
        task_id: str,
        *,
        journal: TaskJournal,
        execution: Any = None,
        verification: Any = None,
        decision: Optional[str] = None,
        recovery_state: Optional[str] = None,
    ) -> TaskRecord:
        """Write a terminal outcome. Writes at most once per task.

        A second call returns the record already stored, so a caller that wrote
        and then failed to acknowledge cannot produce two outcomes.
        """

        existing = self.get(_text(task_id, "task_id"))
        if existing is not None and existing.is_terminal:
            return existing

        history = list(journal.history()) if journal is not None else [COMPLETED]
        steps = list(journal.steps()) if journal is not None else [COMPLETED]
        # ``TaskRecord.request`` is already decoded, so it is copied rather than
        # parsed again.
        request = dict(existing.request) if existing is not None else {}
        request["steps"] = steps
        return self._update(
            task_id,
            {
                "state": journal.state,
                "state_history": json.dumps(history),
                "request": json.dumps(request, sort_keys=True),
                "execution": json.dumps(
                    execution.to_dict() if execution is not None else None, sort_keys=True
                ),
                "verification": json.dumps(
                    _verification_summary(verification), sort_keys=True
                ),
                "decision": decision,
                "recovery_state": recovery_state,
                "finished_at": _now(),
            },
            terminal=True,
        )

    def fail(self, task_id: str, *, reason: str, history: Sequence[str] = ()) -> TaskRecord:
        """Record a task that stopped on an error. Writes at most once."""

        identifier = _text(task_id, "task_id")
        existing = self.get(identifier)
        if existing is not None and existing.is_terminal:
            return existing
        return self._update(
            identifier,
            {
                "state": FAILED,
                "state_history": json.dumps(list(history) or [FAILED]),
                "request": json.dumps(
                    {
                        **(dict(existing.request) if existing is not None else {}),
                        "failure": _text(reason, "reason"),
                    },
                    sort_keys=True,
                ),
                "finished_at": _now(),
            },
            terminal=True,
        )

    # -- reading -----------------------------------------------------------

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """Return one stored task, or ``None``."""

        row = self._connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def all(self) -> Tuple[TaskRecord, ...]:
        """Return every stored task, oldest first."""

        rows = self._connection.execute(
            "SELECT * FROM tasks ORDER BY created_at, task_id"
        ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def interrupted(self) -> Tuple[TaskRecord, ...]:
        """Return tasks recorded mid-execution, which fail closed."""

        rows = self._connection.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY created_at", (EXECUTING,)
        ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    # -- internals ---------------------------------------------------------

    def _update(
        self, task_id: str, values: Dict[str, Any], *, terminal: bool
    ) -> TaskRecord:
        identifier = _text(task_id, "task_id")
        columns = ", ".join(f"{name} = ?" for name in values)
        parameters = list(values.values()) + [_now(), identifier]
        self._connection.execute(
            f"UPDATE tasks SET {columns}, updated_at = ? WHERE task_id = ?",  # noqa: S608
            parameters,
        )
        self._connection.commit()
        record = self.get(identifier)
        if record is None:
            raise ValueError(f"task {identifier!r} was not recorded before updating it")
        del terminal
        return record


def open_task_store(database_path: Any) -> TaskStore:
    """Open (and create if needed) a task store at ``database_path``."""

    return TaskStore(database_path)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _verification_summary(verification: Any) -> Optional[Dict[str, Any]]:
    """Reduce a verification report to counts and status, not case content."""

    if verification is None:
        return None
    return {
        "status": verification.status,
        "total_cases": verification.total_cases,
        "passed_cases": verification.passed_cases,
        "failed_cases": verification.failed_cases,
        "inconclusive_cases": verification.inconclusive_cases,
        "skipped_cases": verification.skipped_cases,
    }


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    def loads(value: Any, default: Any) -> Any:
        if value is None:
            return default
        return json.loads(value)

    plan = loads(row["plan"], None)
    return TaskRecord(
        task_id=row["task_id"],
        repository_name=row["repository_name"],
        state=row["state"],
        request=loads(row["request"], {}),
        plan=tuple(plan) if plan else None,
        execution=loads(row["execution"], None),
        verification=loads(row["verification"], None),
        decision=row["decision"],
        recovery_state=row["recovery_state"],
        state_history=tuple(loads(row["state_history"], [])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )
