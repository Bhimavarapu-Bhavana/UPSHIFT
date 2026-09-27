"""Deterministic recovery orchestration over already-produced UPSHIFT evidence.

The engine is a pure orchestration layer. It consumes the reports that the
existing Phase 4-7 components already produced and never recomputes impact
analysis, risk analysis, verification, or the migration decision. It does not
re-import those analyses, does not read the repository, and does not execute
candidate code, migrations, shell commands, or Git.

The single safety guarantee this layer exists to provide:

    A migration whose independent verification failed is never left in an
    accepted state.

Correctness of the recovery outcome is decided only by independent
verification evidence, both for the candidate and for the restored baseline.
Risk level is carried as context and never determines a recovery state.

Restoration is delegated to a narrow, injected :class:`RollbackProvider`. The
provider abstraction exposes exactly one operation, ``restore_baseline``, for
the single controlled restoration the benchmark requires. The engine never
receives a command, a shell string, an arbitrary filesystem path, or a callable
to execute, and it never performs an autonomous retry loop: the recovery path
is a fixed, straight-line sequence with at most one rollback attempt and at most
one baseline verification.

Replanning is a proposal. When recovery cannot establish a verified baseline,
the engine emits a :class:`ReplanRequest` describing the evidence and suggested
investigation areas. Producing that request never modifies the repository and
never schedules another migration attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Protocol, Sequence, Tuple, runtime_checkable

from ..verification.verifier import (
    FAIL as VERIFICATION_FAIL,
    INCONCLUSIVE as VERIFICATION_INCONCLUSIVE,
    PASS as VERIFICATION_PASS,
    CandidateVerifier,
    VerificationCase,
    VerificationCandidate,
    VerificationReport,
)
from .decision_engine import (
    ACCEPT as DECISION_ACCEPT,
    INCONCLUSIVE as DECISION_INCONCLUSIVE,
    REJECT as DECISION_REJECT,
    DecisionReport,
)

__all__ = [
    "ACCEPTED",
    "COMPLETED",
    "EVIDENCE_DECISION",
    "EVIDENCE_IMPACT",
    "EVIDENCE_RECOVERY",
    "EVIDENCE_RISK",
    "EVIDENCE_ROLLBACK",
    "EVIDENCE_ROLLBACK_VERIFICATION",
    "EVIDENCE_VERIFICATION",
    "INCONCLUSIVE",
    "NOT_RUN",
    "RECOVERED_BASELINE_NOTE",
    "RECOVERY_FAILED",
    "RECOVERY_STATES",
    "REPLANNING",
    "ROLLBACK_REQUIRED",
    "ROLLBACK_VERIFICATION",
    "ROLLING_BACK",
    "MigrationRecoveryEngine",
    "RecoveryEvidence",
    "RecoveryInputError",
    "RecoveryReport",
    "ReplanRequest",
    "RollbackProvider",
    "RollbackResult",
    "StateTransition",
]


# --------------------------------------------------------------------------
# Recovery states
#
# The recovery outcome is an explicit state, never a combination of boolean
# flags. Boolean flags cannot represent "rollback was attempted but produced no
# usable baseline" without an ambiguous third meaning.
# --------------------------------------------------------------------------

ACCEPTED = "ACCEPTED"
ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
ROLLING_BACK = "ROLLING_BACK"
ROLLBACK_VERIFICATION = "ROLLBACK_VERIFICATION"
REPLANNING = "REPLANNING"
INCONCLUSIVE = "INCONCLUSIVE"
RECOVERY_FAILED = "RECOVERY_FAILED"
COMPLETED = "COMPLETED"

RECOVERY_STATES: Tuple[str, ...] = (
    ACCEPTED,
    ROLLBACK_REQUIRED,
    ROLLING_BACK,
    ROLLBACK_VERIFICATION,
    REPLANNING,
    INCONCLUSIVE,
    RECOVERY_FAILED,
    COMPLETED,
)

#: Value used when the baseline was never independently verified.
NOT_RUN = "NOT_RUN"

RECOVERED_BASELINE_NOTE = (
    "Recovery restored and verified the previous known-good baseline. The "
    "migration itself remains rejected and must not be reported as successful."
)

_EVIDENCE_IMPACT = "impact"
_EVIDENCE_RISK = "risk"
_EVIDENCE_VERIFICATION = "verification"
_EVIDENCE_DECISION = "decision"
_EVIDENCE_ROLLBACK = "rollback"
_EVIDENCE_ROLLBACK_VERIFICATION = "rollback_verification"
_EVIDENCE_RECOVERY = "recovery"

EVIDENCE_DECISION = _EVIDENCE_DECISION
EVIDENCE_IMPACT = _EVIDENCE_IMPACT
EVIDENCE_RECOVERY = _EVIDENCE_RECOVERY
EVIDENCE_RISK = _EVIDENCE_RISK
EVIDENCE_ROLLBACK = _EVIDENCE_ROLLBACK
EVIDENCE_ROLLBACK_VERIFICATION = _EVIDENCE_ROLLBACK_VERIFICATION
EVIDENCE_VERIFICATION = _EVIDENCE_VERIFICATION

_IMPACT_ATTRIBUTES: Tuple[str, ...] = (
    "migration_name",
    "impacted_files",
    "direct_references",
    "target_references",
)
_RISK_ATTRIBUTES: Tuple[str, ...] = (
    "migration_name",
    "risk_level",
    "risk_score",
)
_VERIFICATION_ATTRIBUTES: Tuple[str, ...] = (
    "candidate_id",
    "status",
    "results",
)
_PROVIDER_OPERATIONS: Tuple[str, ...] = (
    "rollback_available",
    "restore_baseline",
    "load_restored_baseline",
)


class RecoveryInputError(ValueError):
    """Raised when supplied evidence does not satisfy the recovery interface."""


# --------------------------------------------------------------------------
# Controlled rollback abstraction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of one controlled baseline-restoration attempt.

    This type carries only descriptive evidence. It cannot carry a command, a
    shell string, or a filesystem path for the engine to act on.
    """

    attempted: bool
    succeeded: bool
    reason: str
    restored_candidate_id: Optional[str] = None
    mechanism: str = "controlled_restore"

    def __post_init__(self) -> None:
        if not isinstance(self.attempted, bool) or not isinstance(self.succeeded, bool):
            raise ValueError("attempted and succeeded must be booleans")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        if self.attempted and not self.reason.strip():
            raise ValueError("an attempted rollback must explain its outcome")
        if self.succeeded and not (self.restored_candidate_id or "").strip():
            raise ValueError("a successful rollback must name the restored candidate")
        object.__setattr__(self, "reason", self.reason.strip())

    @classmethod
    def unavailable(cls, reason: str) -> "RollbackResult":
        """Return a result describing why no controlled restoration is possible."""

        return cls(attempted=False, succeeded=False, reason=reason, restored_candidate_id=None)

    @classmethod
    def failure(cls, reason: str) -> "RollbackResult":
        """Return a result for an attempted restoration that did not succeed."""

        return cls(attempted=True, succeeded=False, reason=reason, restored_candidate_id=None)

    @property
    def available(self) -> bool:
        """True when a controlled restoration mechanism is usable at all."""

        return self.attempted or bool(self.restored_candidate_id)


@runtime_checkable
class RollbackProvider(Protocol):
    """The narrow, controlled restoration surface the recovery engine may use.

    The abstraction deliberately exposes one restoration operation. It is not a
    generic "run this" interface: there is no ``command``, ``args``, ``script``,
    or ``callable`` parameter anywhere in this contract, so no provider can be
    handed an arbitrary shell command, Python snippet, or filesystem path and
    have it executed.
    """

    def rollback_available(self, candidate_id: str) -> bool:
        """Return whether a controlled restoration exists for ``candidate_id``."""

    def restore_baseline(self, candidate_id: str) -> RollbackResult:
        """Restore the known-good baseline through the controlled mechanism."""

    def load_restored_baseline(self) -> VerificationCandidate:
        """Return the restored baseline as a narrow in-process callable."""


# --------------------------------------------------------------------------
# Immutable recovery value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryEvidence:
    """One categorized piece of evidence considered during recovery."""

    kind: str
    summary: str
    details: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("kind must be a non-empty string")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be a non-empty string")
        object.__setattr__(self, "details", tuple(self.details))


@dataclass(frozen=True)
class StateTransition:
    """One recorded movement through the recovery state machine.

    ``source_state`` of the first recorded transition is the state recovery
    entered from the Phase 7 decision. A transition whose target equals its
    source records a terminal outcome reached without any further movement.
    """

    source_state: str
    target_state: str
    reason: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_state, "source_state"),
            (self.target_state, "target_state"),
        ):
            if value not in RECOVERY_STATES:
                raise ValueError(
                    f"{label} must be one of {RECOVERY_STATES}, got {value!r}"
                )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")


@dataclass(frozen=True)
class ReplanRequest:
    """A structured re-plan proposal.

    This is a proposal, never an action. Building it performs no repository
    modification, schedules no migration attempt, and grants no authority to
    retry. A human or the orchestrating agent decides what to do with it.
    """

    migration_name: str
    candidate_id: str
    failed_verification_cases: Tuple[str, ...]
    risk_evidence: Tuple[str, ...]
    impact_evidence: Tuple[str, ...]
    rollback_result: str
    baseline_verification_status: str
    recommended_investigation_areas: Tuple[str, ...]
    status: str = "proposal"

    def __post_init__(self) -> None:
        for name in ("migration_name", "candidate_id", "rollback_result"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(
            self, "failed_verification_cases", tuple(self.failed_verification_cases)
        )
        object.__setattr__(self, "risk_evidence", tuple(self.risk_evidence))
        object.__setattr__(self, "impact_evidence", tuple(self.impact_evidence))
        object.__setattr__(
            self,
            "recommended_investigation_areas",
            tuple(self.recommended_investigation_areas),
        )
        if self.status != "proposal":
            raise ValueError(
                "a re-plan request is a proposal and its status must stay 'proposal'"
            )


@dataclass(frozen=True)
class RecoveryReport:
    """Immutable record of one recovery attempt.

    The report is a plain frozen value: it holds no engine reference, no
    provider, and no live object, so a report cannot be used to reach back into
    the system and change an outcome.
    """

    migration_name: str
    initial_candidate_id: str
    initial_decision: str
    initial_verification_status: str
    recovery_state: str
    rollback_attempted: bool
    rollback_succeeded: bool
    rollback_verification_status: str
    final_verification_status: str
    final_candidate_id: str
    replanning_required: bool
    explanations: Tuple[str, ...]
    evidence: Tuple[RecoveryEvidence, ...]
    transitions: Tuple[StateTransition, ...]
    replan_request: Optional[ReplanRequest] = None

    def __post_init__(self) -> None:
        if self.recovery_state not in RECOVERY_STATES:
            raise ValueError(
                f"recovery_state must be one of {RECOVERY_STATES}, "
                f"got {self.recovery_state!r}"
            )
        object.__setattr__(self, "explanations", tuple(self.explanations))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        if not self.transitions:
            raise ValueError("a recovery report must record at least one transition")
        if self.rollback_succeeded and not self.rollback_attempted:
            raise ValueError("rollback_succeeded requires an attempted rollback")
        if (
            self.recovery_state == COMPLETED
            and self.initial_decision != DECISION_ACCEPT
            and self.rollback_verification_status != VERIFICATION_PASS
        ):
            raise ValueError(
                "a recovered migration may only be COMPLETED when the restored "
                "baseline was independently verified, got "
                f"{self.rollback_verification_status!r}"
            )
        if self.replanning_required and self.replan_request is None:
            raise ValueError("replanning_required requires a replan_request")

    @property
    def entry_state(self) -> str:
        """The recovery state entered from the Phase 7 decision."""

        return self.transitions[0].source_state

    @property
    def state_history(self) -> Tuple[str, ...]:
        """Every recovery state visited, in order."""

        history = [self.transitions[0].source_state]
        for transition in self.transitions:
            history.append(transition.target_state)
        return tuple(history)

    @property
    def succeeded(self) -> bool:
        """True only when recovery ended in a verified, accounted-for state."""

        return self.recovery_state in (COMPLETED, INCONCLUSIVE)

    @property
    def accepted(self) -> bool:
        """True when the migration itself was accepted, not merely recovered."""

        return self.recovery_state == COMPLETED and self.initial_decision == DECISION_ACCEPT

    def evidence_of_kind(self, kind: str) -> Tuple[RecoveryEvidence, ...]:
        return tuple(item for item in self.evidence if item.kind == kind)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


def _require_attributes(report: Any, attributes: Sequence[str], label: str) -> None:
    if report is None:
        raise RecoveryInputError(f"{label} report is required")
    missing = [name for name in attributes if not hasattr(report, name)]
    if missing:
        raise RecoveryInputError(
            f"{label} report is missing required attribute(s): {', '.join(missing)}"
        )


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryInputError(f"{label} must be a non-empty string")
    return value.strip()


def _require_provider(provider: Any) -> RollbackProvider:
    missing = [name for name in _PROVIDER_OPERATIONS if not hasattr(provider, name)]
    if missing:
        raise RecoveryInputError(
            "rollback provider must expose only the controlled restoration "
            f"operations; missing: {', '.join(missing)}"
        )
    return provider


class MigrationRecoveryEngine:
    """Drives a failed migration to a verified outcome, or to a proposal.

    The engine composes the existing Phase 6 verifier and the existing Phase 7
    decision report. It does not reimplement either one.
    """

    def __init__(self, verifier: Optional[CandidateVerifier] = None) -> None:
        self._verifier = verifier if verifier is not None else CandidateVerifier()

    # -- public API --------------------------------------------------------

    def recover(
        self,
        impact_report: Any,
        risk_report: Any,
        verification_report: VerificationReport,
        decision_report: DecisionReport,
        rollback_provider: RollbackProvider,
        verification_cases: Sequence[VerificationCase],
    ) -> RecoveryReport:
        """Recover a failed migration, or explain why it cannot be recovered.

        Performs at most one controlled rollback attempt and at most one
        independent baseline verification. Never retries, and never modifies the
        repository except through the injected controlled provider.
        """

        _require_attributes(impact_report, _IMPACT_ATTRIBUTES, "impact")
        _require_attributes(risk_report, _RISK_ATTRIBUTES, "risk")
        _require_attributes(verification_report, _VERIFICATION_ATTRIBUTES, "verification")
        _require_attributes(decision_report, ("decision", "candidate_id"), "decision")
        provider = _require_provider(rollback_provider)

        migration_name = self._shared_migration_name(
            impact_report, risk_report, verification_report, decision_report
        )
        initial_status = _text(verification_report.status, "initial verification status")
        initial_candidate = _text(verification_report.candidate_id, "initial candidate id")
        initial_decision = _text(decision_report.decision, "initial decision")

        if _text(decision_report.candidate_id, "decision candidate id") != initial_candidate:
            raise RecoveryInputError(
                "decision and verification reports must describe the same "
                f"candidate, got {decision_report.candidate_id!r} and {initial_candidate!r}"
            )

        self._guard_against_unsupported_acceptance(initial_status, initial_decision)

        context = _RecoveryContext(
            migration_name=migration_name,
            initial_candidate_id=initial_candidate,
            initial_decision=initial_decision,
            initial_status=initial_status,
            risk_report=risk_report,
            impact_report=impact_report,
            verification_report=verification_report,
        )

        return self._run_recovery(
            context=context,
            provider=provider,
            verification_cases=tuple(verification_cases),
        )

    # -- safety guards -----------------------------------------------------

    @staticmethod
    def _guard_against_unsupported_acceptance(
        verification_status: str, decision: str
    ) -> None:
        """Refuse to proceed if evidence claims success without a PASS.

        This is the structural reason a failed migration cannot be accepted: the
        only route to ``ACCEPTED`` requires an independently verified ``PASS``,
        and a decision claiming otherwise is rejected as malformed evidence
        instead of being honored.
        """

        expected = {
            VERIFICATION_PASS: DECISION_ACCEPT,
            VERIFICATION_FAIL: DECISION_REJECT,
            VERIFICATION_INCONCLUSIVE: DECISION_INCONCLUSIVE,
        }.get(verification_status)
        if expected is None:
            raise RecoveryInputError(
                f"unknown verification status {verification_status!r}"
            )
        if decision != expected:
            raise RecoveryInputError(
                f"verification status {verification_status} cannot support decision "
                f"{decision!r}; it supports {expected!r}. Recovery will not accept a "
                "migration that independent verification did not pass."
            )

    # -- state machine -----------------------------------------------------

    def _run_recovery(
        self,
        *,
        context: "_RecoveryContext",
        provider: RollbackProvider,
        verification_cases: Tuple[VerificationCase, ...],
    ) -> RecoveryReport:
        transitions: list[StateTransition] = []
        state = self._entry_state(context)

        if state == ACCEPTED:
            transitions.append(
                StateTransition(
                    source_state=ACCEPTED,
                    target_state=COMPLETED,
                    reason=(
                        f"Verification status {context.initial_status} supports decision "
                        f"{context.initial_decision}; no recovery action is required."
                    ),
                )
            )
            state = COMPLETED
            return self._build_report(
                context=context,
                state=state,
                transitions=tuple(transitions),
                rollback=RollbackResult.unavailable(
                    "Recovery was not required because independent verification passed."
                ),
                baseline_status=NOT_RUN,
                final_status=context.initial_status,
                final_candidate=context.initial_candidate_id,
                replanning_required=False,
                replan_request=None,
            )

        if state == INCONCLUSIVE:
            transitions.append(
                StateTransition(
                    source_state=INCONCLUSIVE,
                    target_state=INCONCLUSIVE,
                    reason=(
                        f"Verification status {context.initial_status} did not provide "
                        "sufficient required evidence, so no acceptance and no rollback "
                        "is justified."
                    ),
                )
            )
            return self._build_report(
                context=context,
                state=INCONCLUSIVE,
                transitions=tuple(transitions),
                rollback=RollbackResult.unavailable(
                    "No rollback was attempted because verification was inconclusive."
                ),
                baseline_status=NOT_RUN,
                final_status=context.initial_status,
                final_candidate=context.initial_candidate_id,
                replanning_required=False,
                replan_request=None,
            )

        # state is ROLLBACK_REQUIRED: verification failed.
        return self._recover_failed_migration(
            context=context,
            provider=provider,
            verification_cases=verification_cases,
            transitions=transitions,
        )

    def _recover_failed_migration(
        self,
        *,
        context: "_RecoveryContext",
        provider: RollbackProvider,
        verification_cases: Tuple[VerificationCase, ...],
        transitions: list[StateTransition],
    ) -> RecoveryReport:
        """Drive one failed migration through the rollback path.

        Straight-line by construction: availability check, at most one restore,
        at most one independent baseline verification. There is no loop, so an
        autonomous retry cannot be introduced here.
        """

        candidate_id = context.initial_candidate_id

        if not self._rollback_is_available(provider, candidate_id):
            transitions.append(
                StateTransition(
                    source_state=ROLLBACK_REQUIRED,
                    target_state=REPLANNING,
                    reason=(
                        f"No controlled rollback mechanism is available for candidate "
                        f"{candidate_id!r}, so a verified baseline cannot be restored."
                    ),
                )
            )
            transitions.append(
                StateTransition(
                    source_state=REPLANNING,
                    target_state=INCONCLUSIVE,
                    reason=(
                        "Replanning was recorded as a proposal only; the repository was "
                        "not modified and no new attempt was scheduled."
                    ),
                )
            )
            rollback = RollbackResult.unavailable(
                f"No controlled rollback mechanism is available for candidate {candidate_id!r}."
            )
            replan = self._build_replan_request(
                context=context,
                rollback=rollback,
                baseline_status=NOT_RUN,
            )
            return self._build_report(
                context=context,
                state=INCONCLUSIVE,
                transitions=tuple(transitions),
                rollback=rollback,
                baseline_status=NOT_RUN,
                final_status=context.initial_status,
                final_candidate=candidate_id,
                replanning_required=True,
                replan_request=replan,
            )

        transitions.append(
            StateTransition(
                source_state=ROLLBACK_REQUIRED,
                target_state=ROLLING_BACK,
                reason=(
                    f"Independent verification status {context.initial_status} requires "
                    f"restoring the known-good baseline for candidate {candidate_id!r}."
                ),
            )
        )

        rollback = self._attempt_restore(provider, candidate_id)

        if not rollback.succeeded:
            transitions.append(
                StateTransition(
                    source_state=ROLLING_BACK,
                    target_state=RECOVERY_FAILED,
                    reason=(
                        f"Controlled rollback did not restore a usable baseline: "
                        f"{rollback.reason}"
                    ),
                )
            )
            replan = self._build_replan_request(
                context=context, rollback=rollback, baseline_status=NOT_RUN
            )
            return self._build_report(
                context=context,
                state=RECOVERY_FAILED,
                transitions=tuple(transitions),
                rollback=rollback,
                baseline_status=NOT_RUN,
                final_status=context.initial_status,
                final_candidate=candidate_id,
                replanning_required=False,
                replan_request=replan,
            )

        transitions.append(
            StateTransition(
                source_state=ROLLING_BACK,
                target_state=ROLLBACK_VERIFICATION,
                reason=(
                    "The baseline was restored through the controlled mechanism and must "
                    "now be verified independently before recovery can be trusted."
                ),
            )
        )

        baseline_status, baseline_failure = self._verify_restored_baseline(
            provider=provider, verification_cases=verification_cases, context=context
        )

        if baseline_status == VERIFICATION_PASS:
            transitions.append(
                StateTransition(
                    source_state=ROLLBACK_VERIFICATION,
                    target_state=COMPLETED,
                    reason=(
                        f"Independent verification of the restored baseline returned "
                        f"{VERIFICATION_PASS}, so the known-good state is restored."
                    ),
                )
            )
            return self._build_report(
                context=context,
                state=COMPLETED,
                transitions=tuple(transitions),
                rollback=rollback,
                baseline_status=VERIFICATION_PASS,
                final_status=VERIFICATION_PASS,
                final_candidate=_text(
                    rollback.restored_candidate_id, "restored candidate id"
                ),
                replanning_required=False,
                replan_request=None,
            )

        transitions.append(
            StateTransition(
                source_state=ROLLBACK_VERIFICATION,
                target_state=RECOVERY_FAILED,
                reason=(
                    "Independent verification of the restored baseline did not pass "
                    f"({baseline_failure or baseline_status}), so the baseline cannot "
                    "be claimed as restored."
                ),
            )
        )
        replan = self._build_replan_request(
            context=context, rollback=rollback, baseline_status=baseline_status
        )
        return self._build_report(
            context=context,
            state=RECOVERY_FAILED,
            transitions=tuple(transitions),
            rollback=rollback,
            baseline_status=baseline_status,
            final_status=baseline_status,
            final_candidate=_text(rollback.restored_candidate_id, "restored candidate id"),
            replanning_required=False,
            replan_request=replan,
        )

    @staticmethod
    def _entry_state(context: "_RecoveryContext") -> str:
        if context.initial_status == VERIFICATION_PASS:
            return ACCEPTED
        if context.initial_status == VERIFICATION_INCONCLUSIVE:
            return INCONCLUSIVE
        return ROLLBACK_REQUIRED

    @staticmethod
    def _rollback_is_available(provider: RollbackProvider, candidate_id: str) -> bool:
        try:
            return bool(provider.rollback_available(candidate_id))
        except Exception as error:  # a broken provider must not look like success
            raise RecoveryInputError(
                "rollback provider failed to report availability: "
                f"{type(error).__name__}: {error}"
            ) from error

    @staticmethod
    def _attempt_restore(provider: RollbackProvider, candidate_id: str) -> RollbackResult:
        """Perform the single allowed restoration attempt."""

        try:
            rollback = provider.restore_baseline(candidate_id)
        except Exception as error:
            return RollbackResult.failure(
                f"the controlled rollback mechanism raised {type(error).__name__}"
            )
        if not isinstance(rollback, RollbackResult):
            raise RecoveryInputError(
                "rollback provider must return a RollbackResult, got "
                f"{type(rollback).__name__}"
            )
        return rollback

    def _verify_restored_baseline(
        self,
        *,
        provider: RollbackProvider,
        verification_cases: Tuple[VerificationCase, ...],
        context: "_RecoveryContext",
    ) -> Tuple[str, Optional[str]]:
        """Re-verify the restored baseline with the existing Phase 6 verifier."""

        try:
            candidate = provider.load_restored_baseline()
        except Exception as error:
            return (
                VERIFICATION_FAIL,
                f"the restored baseline could not be loaded "
                f"({type(error).__name__})",
            )
        if not isinstance(candidate, VerificationCandidate):
            return (
                VERIFICATION_FAIL,
                "the rollback provider did not return a VerificationCandidate",
            )
        if not verification_cases:
            return (
                VERIFICATION_FAIL,
                "no verification cases were supplied for the baseline check",
            )

        try:
            report = self._verifier.verify(
                candidate, verification_cases, migration_name=context.migration_name
            )
        except Exception as error:
            return (
                VERIFICATION_FAIL,
                f"baseline verification raised {type(error).__name__}",
            )
        return (report.status, self._first_failure(report))

    @staticmethod
    def _first_failure(report: VerificationReport) -> Optional[str]:
        for result in report.results:
            if result.status == VERIFICATION_FAIL:
                return (
                    f"case {result.case_id} expected {result.expected!r} but observed "
                    f"{result.observed!r}"
                )
        return None

    # -- report construction ----------------------------------------------

    @staticmethod
    def _shared_migration_name(*reports: Any) -> str:
        names = tuple(_text(report.migration_name, "migration_name") for report in reports)
        if len(set(names)) != 1:
            raise RecoveryInputError(
                "all reports must describe the same migration, got: "
                + ", ".join(repr(name) for name in names)
            )
        return names[0]

    def _build_replan_request(
        self,
        *,
        context: "_RecoveryContext",
        rollback: RollbackResult,
        baseline_status: str,
    ) -> ReplanRequest:
        """Build a deterministic, proposal-only re-plan request."""

        failed = tuple(
            result.case_id
            for result in context.verification_report.results
            if result.status == VERIFICATION_FAIL
        )
        risk_evidence = tuple(
            f"Risk level {context.risk_report.risk_level} with score "
            f"{context.risk_report.risk_score} (complexity evidence only; it does "
            "not determine correctness)."
        )
        impact_evidence = (
            f"Impact evidence: {len(context.impact_report.impacted_files)} impacted "
            f"file(s), {len(context.impact_report.direct_references)} direct OLD "
            f"reference(s), {len(context.impact_report.target_references)} TARGET "
            "reference(s)."
        )

        areas = [
            "Re-run independent verification before any further migration attempt.",
        ]
        for result in context.verification_report.results:
            if result.status != VERIFICATION_FAIL:
                continue
            areas.append(
                f"Investigate case {result.case_id}: expected {result.expected!r} but "
                f"observed {result.observed!r}."
            )
        if not baseline_status == VERIFICATION_PASS:
            areas.append(
                f"Confirm a verified baseline is available before retrying; the last "
                f"baseline verification status was {baseline_status}."
            )
        if not rollback.succeeded:
            areas.append(
                f"Establish a working controlled rollback mechanism: {rollback.reason}"
            )

        return ReplanRequest(
            migration_name=context.migration_name,
            candidate_id=context.initial_candidate_id,
            failed_verification_cases=failed,
            risk_evidence=risk_evidence,
            impact_evidence=impact_evidence,
            rollback_result=rollback.reason,
            baseline_verification_status=baseline_status,
            recommended_investigation_areas=tuple(areas),
        )

    def _build_report(
        self,
        *,
        context: "_RecoveryContext",
        state: str,
        transitions: Tuple[StateTransition, ...],
        rollback: RollbackResult,
        baseline_status: str,
        final_status: str,
        final_candidate: str,
        replanning_required: bool,
        replan_request: Optional[ReplanRequest],
    ) -> RecoveryReport:
        evidence = [
            RecoveryEvidence(
                kind=EVIDENCE_VERIFICATION,
                summary=(
                    f"Initial verification status {context.initial_status} for candidate "
                    f"{context.initial_candidate_id!r}: "
                    f"{context.verification_report.passed_cases} passed, "
                    f"{context.verification_report.failed_cases} failed, "
                    f"{context.verification_report.inconclusive_cases} inconclusive, "
                    f"{context.verification_report.skipped_cases} skipped across "
                    f"{context.verification_report.total_cases} case(s)."
                ),
                details=tuple(context.verification_report.failure_evidence),
            ),
            RecoveryEvidence(
                kind=EVIDENCE_DECISION,
                summary=(
                    f"Phase 7 decision {context.initial_decision} was carried into "
                    "recovery unchanged; recovery never re-evaluates the decision."
                ),
            ),
            RecoveryEvidence(
                kind=EVIDENCE_RISK,
                summary=(
                    f"Risk level {context.risk_report.risk_level} with score "
                    f"{context.risk_report.risk_score} (context only; it never "
                    "determines a recovery state)."
                ),
            ),
            RecoveryEvidence(
                kind=EVIDENCE_IMPACT,
                summary=(
                    f"Impact evidence: {len(context.impact_report.impacted_files)} "
                    f"impacted file(s), "
                    f"{len(context.impact_report.direct_references)} direct OLD "
                    f"reference(s), "
                    f"{len(context.impact_report.target_references)} TARGET reference(s)."
                ),
            ),
            RecoveryEvidence(
                kind=EVIDENCE_ROLLBACK,
                summary=(
                    f"Controlled rollback attempted={rollback.attempted}, "
                    f"succeeded={rollback.succeeded} via {rollback.mechanism}: "
                    f"{rollback.reason}"
                ),
            ),
            RecoveryEvidence(
                kind=EVIDENCE_ROLLBACK_VERIFICATION,
                summary=(
                    f"Independent verification of the restored baseline returned "
                    f"{baseline_status}."
                ),
            ),
            RecoveryEvidence(
                kind=EVIDENCE_RECOVERY,
                summary=(
                    f"Recovery ended in state {state} after visiting "
                    f"{self._history_summary(transitions)}."
                ),
            ),
        ]

        return RecoveryReport(
            migration_name=context.migration_name,
            initial_candidate_id=context.initial_candidate_id,
            initial_decision=context.initial_decision,
            initial_verification_status=context.initial_status,
            recovery_state=state,
            rollback_attempted=rollback.attempted,
            rollback_succeeded=rollback.succeeded,
            rollback_verification_status=baseline_status,
            final_verification_status=final_status,
            final_candidate_id=final_candidate,
            replanning_required=replanning_required,
            explanations=self._explanations(
                context=context,
                state=state,
                transitions=transitions,
                rollback=rollback,
                baseline_status=baseline_status,
                final_status=final_status,
            ),
            evidence=tuple(evidence),
            transitions=transitions,
            replan_request=replan_request,
        )

    @staticmethod
    def _history_summary(transitions: Tuple[StateTransition, ...]) -> str:
        history = [transitions[0].source_state]
        for transition in transitions:
            history.append(transition.target_state)
        return " -> ".join(history)

    def _explanations(
        self,
        *,
        context: "_RecoveryContext",
        state: str,
        transitions: Tuple[StateTransition, ...],
        rollback: RollbackResult,
        baseline_status: str,
        final_status: str,
    ) -> Tuple[str, ...]:
        """Build deterministic explanations in a fixed order."""

        primary = {
            ACCEPTED: (
                "Independent verification passed and the decision is ACCEPT, so the "
                "migration is accepted without recovery."
            ),
            COMPLETED: (
                f"Independent verification status {final_status} establishes a verified "
                "final state for this migration."
            ),
            ROLLBACK_REQUIRED: (
                f"Independent verification status {context.initial_status} means the "
                "migration cannot be accepted."
            ),
            ROLLING_BACK: "A controlled baseline restoration is in progress.",
            ROLLBACK_VERIFICATION: (
                "The restored baseline is being verified independently before recovery "
                "can be trusted."
            ),
            REPLANNING: (
                "Recovery could not establish a verified baseline, so a re-plan was "
                "recorded as a proposal only."
            ),
            INCONCLUSIVE: (
                "Recovery is inconclusive: no verified baseline was established and no "
                "acceptance is claimed."
            ),
            RECOVERY_FAILED: (
                "Recovery failed: a verified baseline could not be established, so the "
                "migration is not accepted and the state is not reported as successful."
            ),
        }[state]

        notes = [
            primary,
            f"State path: {self._history_summary(transitions)}.",
            (
                f"Initial verification status was {context.initial_status} and the Phase 7 "
                f"decision was {context.initial_decision}; recovery did not re-evaluate "
                "either one."
            ),
            (
                f"Rollback attempted={rollback.attempted} and succeeded="
                f"{rollback.succeeded}: {rollback.reason}"
            ),
            (
                f"Independent verification of the restored baseline returned "
                f"{baseline_status}; recovery trusts only verification evidence."
            ),
            (
                f"Risk level {context.risk_report.risk_level} was carried as context and "
                "did not determine this recovery state."
            ),
        ]
        if state == COMPLETED and context.initial_decision != DECISION_ACCEPT:
            notes.append(RECOVERED_BASELINE_NOTE)
        return tuple(notes)


@dataclass(frozen=True)
class _RecoveryContext:
    """Internal, immutable bundle of the evidence recovery coordinates."""

    migration_name: str
    initial_candidate_id: str
    initial_decision: str
    initial_status: str
    impact_report: Any
    risk_report: Any
    verification_report: Any
