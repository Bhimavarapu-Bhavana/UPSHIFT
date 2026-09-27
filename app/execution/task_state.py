"""Task states and the event journal they are derived from.

Phase 13 adds the *observable* part of a real-time loop. The important property
is that no state is ever reported unless it was actually reached: there is no
progress estimate, no percentage, no timer, and no smoothing. A state's only
source of truth is a :class:`TaskEvent` that some real step appended when it
finished or failed.

The states
----------
``PENDING``     accepted as a request; no work has started.
``ANALYZING``   the existing bounded reader and Phase 4/5 engines are running.
``PLANNING``    operations are being validated against the live filesystem.
``EXECUTING``   the bounded executor is writing.
``VERIFYING``   the existing Phase 6 verifier is running over the changed files.
``COMPLETED``   the run finished and its outcome is decided.
``FAILED``      the run stopped on an error; ``FAILED`` is terminal for the task.
``RECOVERING``  the existing Phase 9 recovery engine is running.
``ROLLED_BACK`` recovery restored the pre-execution baseline.

Only ``COMPLETED``, ``FAILED``, and ``ROLLED_BACK`` are terminal, and each task
reaches exactly one of them. A state is never revised after it is recorded, so
the journal is an append-only history rather than a mutable progress bar, and
replaying it reproduces the reported state exactly.

Why a journal and not a field
-----------------------------
A single ``state`` field cannot distinguish "executing" from "finished
executing and about to verify". The journal can, and it also records *which*
step produced each transition and whether it succeeded, which is what the
dashboard needs in order to show that a state is real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "ANALYZING",
    "COMPLETED",
    "EXECUTING",
    "FAILED",
    "PENDING",
    "PLANNING",
    "RECOVERING",
    "ROLLED_BACK",
    "TASK_STATES",
    "TERMINAL_STATES",
    "VERIFYING",
    "TaskEvent",
    "TaskJournal",
    "TaskStateError",
]


PENDING = "PENDING"
ANALYZING = "ANALYZING"
PLANNING = "PLANNING"
EXECUTING = "EXECUTING"
VERIFYING = "VERIFYING"
RECOVERING = "RECOVERING"
COMPLETED = "COMPLETED"
ROLLED_BACK = "ROLLED_BACK"
FAILED = "FAILED"

#: Every state, in the order a healthy pass walks through them.
TASK_STATES: Tuple[str, ...] = (
    PENDING,
    ANALYZING,
    PLANNING,
    EXECUTING,
    VERIFYING,
    COMPLETED,
)

#: States a task can rest in. A task that reaches one of these never moves again.
TERMINAL_STATES: frozenset = frozenset({COMPLETED, ROLLED_BACK, FAILED})


class TaskStateError(ValueError):
    """Raised on an illegal state transition.

    The rule is deliberately strict. A task may skip forward (a plan with no
    operations does not execute), but it may never move backwards out of a
    terminal state, and it may never leave a terminal state at all. That is what
    stops a retry loop from rewriting history.
    """


@dataclass(frozen=True)
class TaskEvent:
    """One real state change, recorded when it happened.

    Attributes:
        state: The state entered.
        step: The name of the operation that caused it. Not prose: it is the
            caller's own label, so it can be matched to the code that ran.
        succeeded: False only for the event that ended the task in ``FAILED``.
        detail: Optional short, non-sensitive context. Never file content.
    """

    state: str
    step: str
    succeeded: bool = True
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in TASK_STATES and self.state not in {
            RECOVERING,
            ROLLED_BACK,
            FAILED,
        }:
            raise TaskStateError(f"unknown task state {self.state!r}")
        if not isinstance(self.step, str) or not self.step.strip():
            raise TaskStateError("a task event must name the step that caused it")
        if not isinstance(self.detail, str):
            raise TaskStateError("task event detail must be text")
        object.__setattr__(self, "step", self.step.strip())
        object.__setattr__(self, "detail", self.detail.strip())

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable projection."""

        return {
            "state": self.state,
            "step": self.step,
            "succeeded": self.succeeded,
            "detail": self.detail,
        }


@dataclass
class TaskJournal:
    """Append-only history of one task's real state changes.

    The reported state is always the last event, never a stored field that could
    drift from the history. :meth:`state` therefore cannot disagree with
    :meth:`history`, and both survive a restart once the journal has been
    persisted.
    """

    task_id: str
    _events: List[TaskEvent] = field(default_factory=list, repr=False)

    def record(self, state: str, step: str, *, succeeded: bool = True, detail: str = "") -> TaskEvent:
        """Append one event and return it.

        Raises:
            TaskStateError: If the transition is illegal, including any attempt
                to move out of a terminal state.
        """

        if self._events and self._events[-1].state in TERMINAL_STATES:
            raise TaskStateError(
                f"task {self.task_id!r} already reached the terminal state "
                f"{self._events[-1].state} and cannot move to {state}"
            )
        event = TaskEvent(state=state, step=step, succeeded=succeeded, detail=detail)
        self._events.append(event)
        return event

    def run(self, state: str, step: str, action: Callable[[], Any], *, detail: str = "") -> Any:
        """Record a state, run one real step, and record its outcome.

        The step's own exception is not swallowed. If it raises, the task is
        marked ``FAILED`` with the exception's type as the detail and the
        exception is re-raised, so a caller cannot mistake a crashed step for a
        completed one.
        """

        self.record(state, step, detail=detail)
        try:
            result = action()
        except Exception as error:  # noqa: BLE001 - re-raised immediately below
            self.record(
                FAILED,
                step,
                succeeded=False,
                detail=type(error).__name__,
            )
            raise
        return result

    @property
    def state(self) -> str:
        """The last recorded state, or ``PENDING`` before anything happened."""

        return self._events[-1].state if self._events else PENDING

    @property
    def is_terminal(self) -> bool:
        """True when the task has come to rest."""

        return bool(self._events) and self._events[-1].state in TERMINAL_STATES

    def history(self) -> Tuple[str, ...]:
        """Every state the task actually passed through, in order."""

        return tuple(event.state for event in self._events)

    def steps(self) -> Tuple[str, ...]:
        """The step name behind each recorded state."""

        return tuple(event.step for event in self._events)

    def events(self) -> Tuple[TaskEvent, ...]:
        """The full journal."""

        return tuple(self._events)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable projection of the whole journal."""

        return {
            "task_id": self.task_id,
            "state": self.state,
            "terminal": self.is_terminal,
            "history": list(self.history()),
            "events": [event.to_dict() for event in self._events],
        }

    @classmethod
    def replay(
        cls, task_id: str, events: Iterable[Any], *, step_for_state: Optional[Dict[str, str]] = None
    ) -> "TaskJournal":
        """Rebuild a journal from persisted events after a restart.

        Replay is what makes a completed task still completed: the states are
        not recomputed, they are read back, so a restart cannot invent progress
        or lose it.
        """

        journal = cls(task_id=task_id)
        for raw in events:
            if isinstance(raw, TaskEvent):
                event = raw
            elif isinstance(raw, dict):
                data = dict(raw)
                event = TaskEvent(
                    state=data.get("state", ""),
                    step=data.get("step", ""),
                    succeeded=bool(data.get("succeeded", True)),
                    detail=str(data.get("detail", "")),
                )
            else:
                raise TaskStateError("a persisted event must be a mapping or TaskEvent")
            journal._events.append(event)
        del step_for_state
        return journal
