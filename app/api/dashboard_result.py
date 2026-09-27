"""Presentation-only result model for the UPSHIFT dashboard.

Every value here is copied from an existing Phase 4-9 report object. This
module adds no analysis, invents no state, and never recomputes an outcome.
Its only job is to give the dashboard a stable, JSON-serializable shape.

Security boundary enforced here:

* No engine, verifier, provider, or repository handle is stored on a result, so
  a rendered result cannot be used to reach back into the system.
* No absolute filesystem path, temporary workspace name, or clock reading is
  ever placed in a result. Locations are the repository-relative labels the
  existing analyzers already produce.
* ``final_outcome`` is derived strictly from the existing decision and recovery
  state. It can never report a rejected migration as successful.
* Real-repository mode is additive: ``mode`` defaults to the controlled
  benchmark, and ``repository`` is ``None`` there, so a benchmark result is
  byte-for-byte what it was before real-repository mode existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from app.core.decision_engine import (
    ACCEPT,
    INCONCLUSIVE,
    REJECT,
)
from app.core.recovery_engine import COMPLETED, RECOVERY_FAILED, RecoveryReport
from app.security.repository_input import MODE_DEMO, MODE_REAL_REPOSITORY

__all__ = [
    "MODE_DEMO",
    "MODE_REAL_REPOSITORY",
    "OUTCOME_ACCEPTED",
    "OUTCOME_INCONCLUSIVE",
    "OUTCOME_REJECTED",
    "OUTCOME_REJECTED_BASELINE_RESTORED",
    "OUTCOME_REJECTED_RECOVERY_FAILED",
    "OUTCOME_REJECTED_REPLAN_REQUIRED",
    "DecisionSection",
    "DashboardResult",
    "ImpactSection",
    "RecoverySection",
    "RepositorySection",
    "RiskSection",
    "VerificationSection",
    "build_dashboard_result",
]

OUTCOME_ACCEPTED = "MIGRATION_ACCEPTED"
OUTCOME_REJECTED = "MIGRATION_REJECTED"
OUTCOME_REJECTED_BASELINE_RESTORED = "MIGRATION_REJECTED_BASELINE_RESTORED"
OUTCOME_REJECTED_RECOVERY_FAILED = "MIGRATION_REJECTED_RECOVERY_FAILED"
OUTCOME_REJECTED_REPLAN_REQUIRED = "MIGRATION_REJECTED_REPLAN_REQUIRED"
OUTCOME_INCONCLUSIVE = "MIGRATION_INCONCLUSIVE"

_OUTCOME_DETAIL: Mapping[str, str] = {
    OUTCOME_ACCEPTED: (
        "Verification passed and the candidate was accepted."
    ),
    OUTCOME_REJECTED: (
        "The migration was rejected and no verified baseline was restored."
    ),
    OUTCOME_REJECTED_BASELINE_RESTORED: (
        "The migration was rejected. The verified baseline was restored. "
        "This is a safe recovery, not a migration success."
    ),
    OUTCOME_REJECTED_RECOVERY_FAILED: (
        "The migration was rejected and recovery could not restore a "
        "verified baseline. Manual attention is required."
    ),
    OUTCOME_REJECTED_REPLAN_REQUIRED: (
        "The migration was rejected and rollback was unavailable. A "
        "re-planning proposal was produced and remains a proposal."
    ),
    OUTCOME_INCONCLUSIVE: (
        "The migration could not be decided on the available evidence."
    ),
}


def _text_tuple(value: Any) -> Tuple[str, ...]:
    return tuple(str(item) for item in value or ())


def _json_value(value: Any) -> Any:
    """Return a JSON-encodable copy of a verification value.

    Most verification values are already strings, and those are returned
    untouched, so a controlled-benchmark result is unchanged. The verifier also
    uses a sentinel for "no expected behavior is defined", and a real-repository
    case carries that sentinel; it becomes readable text here rather than
    depending on a non-standard encoder.
    """

    if value is None or isinstance(value, (str, int, float, bool, list, tuple, dict)):
        return value
    return str(value)


@dataclass(frozen=True)
class ImpactSection:
    """Phase 4 impact evidence, copied for display."""

    old_api: str
    target_api: str
    impacted_file_count: int
    direct_reference_count: int
    target_reference_count: int
    renamed_symbols: Tuple[Tuple[str, str], ...]
    impacted_files: Tuple[str, ...]
    direct_references: Tuple[str, ...]
    target_references: Tuple[str, ...]
    metadata_listed_files: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "old_api": self.old_api,
            "target_api": self.target_api,
            "impacted_file_count": self.impacted_file_count,
            "direct_reference_count": self.direct_reference_count,
            "target_reference_count": self.target_reference_count,
            "renamed_symbols": [
                {"from": old, "to": new} for old, new in self.renamed_symbols
            ],
            "impacted_files": list(self.impacted_files),
            "direct_references": list(self.direct_references),
            "target_references": list(self.target_references),
            "metadata_listed_files": list(self.metadata_listed_files),
        }


@dataclass(frozen=True)
class RiskSection:
    """Phase 5 risk evidence, copied for display."""

    risk_level: str
    risk_score: int
    risk_factors: Tuple[str, ...]
    explanations: Tuple[str, ...]
    affected_test_files: Tuple[str, ...]
    renamed_symbol_count: int
    #: The same factors in structured form, so a presentation layer can render
    #: them without parsing the human-readable strings above.
    risk_factor_details: Tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "risk_factors": list(self.risk_factors),
            "risk_factor_details": [dict(factor) for factor in self.risk_factor_details],
            "explanations": list(self.explanations),
            "affected_test_files": list(self.affected_test_files),
            "renamed_symbol_count": self.renamed_symbol_count,
        }


@dataclass(frozen=True)
class VerificationSection:
    """Phase 6 verification evidence, copied for display."""

    verification_status: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    inconclusive_cases: int
    skipped_cases: int
    evidence: Tuple[str, ...]
    case_results: Tuple[Mapping[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verification_status": self.verification_status,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "inconclusive_cases": self.inconclusive_cases,
            "skipped_cases": self.skipped_cases,
            "evidence": list(self.evidence),
            "case_results": [dict(result) for result in self.case_results],
        }


@dataclass(frozen=True)
class DecisionSection:
    """Phase 7 decision, copied for display."""

    decision: str
    accepted: bool
    verification_status: str
    risk_level: str
    risk_score: int
    explanations: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "accepted": self.accepted,
            "verification_status": self.verification_status,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "explanations": list(self.explanations),
        }


@dataclass(frozen=True)
class RecoverySection:
    """Phase 9 recovery evidence, copied for display.

    ``migration_accepted`` is carried separately from ``recovery_state`` on
    purpose. Reaching ``COMPLETED`` after a rollback means the baseline was
    restored, not that the migration was accepted, so the original decision is
    reported here as well.
    """

    recovery_state: str
    state_history: Tuple[str, ...]
    transitions: Tuple[str, ...]
    initial_decision: str
    initial_verification_status: str
    rollback_attempted: bool
    rollback_succeeded: bool
    rollback_verification_status: str
    final_verification_status: str
    final_candidate_id: str
    replanning_required: bool
    migration_accepted: bool
    replan_proposal_only: bool
    explanations: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recovery_state": self.recovery_state,
            "state_history": list(self.state_history),
            "transitions": list(self.transitions),
            "initial_decision": self.initial_decision,
            "initial_verification_status": self.initial_verification_status,
            "rollback_attempted": self.rollback_attempted,
            "rollback_succeeded": self.rollback_succeeded,
            "rollback_verification_status": self.rollback_verification_status,
            "final_verification_status": self.final_verification_status,
            "final_candidate_id": self.final_candidate_id,
            "replanning_required": self.replanning_required,
            "migration_accepted": self.migration_accepted,
            "replan_proposal_only": self.replan_proposal_only,
            "explanations": list(self.explanations),
        }


@dataclass(frozen=True)
class RepositorySection:
    """What a real-repository read actually covered, copied for display.

    Present only in ``real_repository`` mode. The absolute repository location is
    deliberately absent: a result never carries a filesystem path, so the
    dashboard can show *what was read* without ever disclosing *where it is*.
    Skipped files are reported with an exact count and a bounded, sorted sample
    of repository-relative labels, so a reader can see both the scale of what
    was left out and which files it was.
    """

    repository_name: str
    inspected_file_count: int
    inspected_paths: Tuple[str, ...]
    inspected_truncated: bool
    skipped_file_count: int
    skipped: Tuple[Mapping[str, Any], ...]
    ignored_directories: Tuple[Tuple[str, int], ...]
    total_bytes_read: int
    limits: Mapping[str, int]
    safety_restrictions: Tuple[str, ...]
    rollback_mechanism: str

    @classmethod
    def from_evidence(cls, evidence: Any, rollback_mechanism: str) -> "RepositorySection":
        """Project a :class:`app.repository.evidence.RepositoryEvidence`."""

        limits = evidence.limits
        return cls(
            repository_name=evidence.repository_name,
            inspected_file_count=evidence.inspected_file_count,
            inspected_paths=evidence.inspected_paths,
            inspected_truncated=evidence.inspected_truncated,
            skipped_file_count=evidence.skipped_file_count,
            skipped=tuple(
                {
                    "reason": summary.reason,
                    "count": summary.count,
                    "recorded_paths": list(summary.recorded_paths),
                    "truncated": summary.truncated,
                }
                for summary in evidence.skipped
            ),
            ignored_directories=tuple(evidence.ignored_directories),
            total_bytes_read=evidence.total_bytes_read,
            limits={
                "max_file_bytes": limits.max_file_bytes,
                "max_total_bytes": limits.max_total_bytes,
                "max_files": limits.max_files,
                "max_directories": limits.max_directories,
            },
            safety_restrictions=evidence.safety_restrictions,
            rollback_mechanism=rollback_mechanism,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repository_name": self.repository_name,
            "inspected_file_count": self.inspected_file_count,
            "inspected_paths": list(self.inspected_paths),
            "inspected_truncated": self.inspected_truncated,
            "skipped_file_count": self.skipped_file_count,
            "skipped": [dict(summary) for summary in self.skipped],
            "ignored_directories": [
                {"name": name, "count": count} for name, count in self.ignored_directories
            ],
            "total_bytes_read": self.total_bytes_read,
            "limits": dict(self.limits),
            "safety_restrictions": list(self.safety_restrictions),
            "rollback_mechanism": self.rollback_mechanism,
        }


@dataclass(frozen=True)
class DashboardResult:
    """One complete, immutable dashboard result."""

    migration_name: str
    candidate_id: str
    final_outcome: str
    final_outcome_detail: str
    impact: ImpactSection
    risk: RiskSection
    verification: VerificationSection
    decision: DecisionSection
    recovery: RecoverySection
    #: Which input produced this result. Defaults to the controlled benchmark,
    #: so a result built without naming a mode is a benchmark result.
    mode: str = MODE_DEMO
    #: Repository read evidence, present only in ``real_repository`` mode.
    repository: Optional[RepositorySection] = None
    #: Real-time task state, present only when a task produced one. This is the
    #: journal of states a run actually reached, so a dashboard can show what
    #: happened without inventing progress. ``None`` for every result that did
    #: not come from :mod:`app.execution.migration_task`.
    task_state: Optional[Mapping[str, Any]] = None

    @property
    def is_real_repository(self) -> bool:
        return self.mode == MODE_REAL_REPOSITORY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "migration_name": self.migration_name,
            "candidate_id": self.candidate_id,
            "final_outcome": self.final_outcome,
            "final_outcome_detail": self.final_outcome_detail,
            "repository": self.repository.to_dict() if self.repository else None,
            "impact": self.impact.to_dict(),
            "risk": self.risk.to_dict(),
            "verification": self.verification.to_dict(),
            "decision": self.decision.to_dict(),
            "recovery": self.recovery.to_dict(),
        }


def _final_outcome(decision: str, recovery: RecoveryReport) -> str:
    if decision == ACCEPT:
        return OUTCOME_ACCEPTED
    if decision == INCONCLUSIVE:
        return OUTCOME_INCONCLUSIVE
    if recovery.replanning_required:
        return OUTCOME_REJECTED_REPLAN_REQUIRED
    if recovery.recovery_state == RECOVERY_FAILED:
        return OUTCOME_REJECTED_RECOVERY_FAILED
    if recovery.recovery_state == COMPLETED:
        return OUTCOME_REJECTED_BASELINE_RESTORED
    return OUTCOME_REJECTED


def build_dashboard_result(
    impact_report: Any,
    risk_report: Any,
    verification_report: Any,
    decision_report: Any,
    recovery_report: RecoveryReport,
    *,
    mode: str = MODE_DEMO,
    repository_evidence: Any = None,
    rollback_mechanism: str = "controlled_benchmark_baseline",
    task_state: Optional[Mapping[str, Any]] = None,
) -> DashboardResult:
    """Project five existing reports into one immutable dashboard result.

    Args:
        impact_report: An existing Phase 4 :class:`ImpactReport`.
        risk_report: An existing Phase 5 :class:`RiskReport`.
        verification_report: An existing Phase 6 :class:`VerificationReport`.
        decision_report: An existing Phase 7 :class:`DecisionReport`.
        recovery_report: An existing Phase 9 :class:`RecoveryReport`.
        mode: Which input produced the reports. The default keeps a benchmark
            result identical to a result built before real-repository mode.
        repository_evidence: Read evidence from
            :mod:`app.repository.evidence`, for real-repository mode only.
        rollback_mechanism: The restoration mechanism the recovery engine had
            available, reported as a fact rather than as a promise.

    Returns:
        A :class:`DashboardResult` holding only copied, JSON-serializable text
        and integers.

    Raises:
        TypeError: If any of the five reports is ``None``; the projection
            refuses to invent a section rather than substituting a placeholder.
        TypeError: If ``repository_evidence`` is supplied in a mode that is not
            real-repository mode, which would mislabel where evidence came from.
    """

    for name, value in (
        ("impact_report", impact_report),
        ("risk_report", risk_report),
        ("verification_report", verification_report),
        ("decision_report", decision_report),
        ("recovery_report", recovery_report),
    ):
        if value is None:
            raise TypeError(f"{name} is required; the dashboard never invents evidence")

    if mode not in (MODE_DEMO, MODE_REAL_REPOSITORY):
        raise TypeError("mode must be the controlled benchmark or real repository")
    if repository_evidence is not None and mode != MODE_REAL_REPOSITORY:
        raise TypeError("repository evidence belongs only to a real-repository result")

    impact = ImpactSection(
        old_api=impact_report.old_api,
        target_api=impact_report.target_api,
        impacted_file_count=len(impact_report.impacted_files),
        direct_reference_count=len(impact_report.direct_references),
        target_reference_count=len(impact_report.target_references),
        renamed_symbols=tuple(
            (old, new) for old, new in impact_report.renamed_symbols
        ),
        impacted_files=tuple(entry.path for entry in impact_report.impacted_files),
        direct_references=tuple(
            f"{reference.path}:{reference.line} {reference.symbol}"
            for reference in impact_report.direct_references
        ),
        target_references=tuple(
            f"{reference.path}:{reference.line} {reference.symbol}"
            for reference in impact_report.target_references
        ),
        metadata_listed_files=_text_tuple(impact_report.metadata_listed_files),
    )

    risk = RiskSection(
        risk_level=risk_report.risk_level,
        risk_score=risk_report.risk_score,
        risk_factors=tuple(
            f"{factor.severity} {factor.category} (+{factor.score}): {factor.reason}"
            for factor in risk_report.risk_factors
        ),
        explanations=_text_tuple(risk_report.explanations),
        affected_test_files=_text_tuple(risk_report.affected_test_files),
        renamed_symbol_count=risk_report.renamed_symbol_count,
        risk_factor_details=tuple(
            {
                "category": factor.category,
                "severity": factor.severity,
                "score": factor.score,
                "reason": factor.reason,
            }
            for factor in risk_report.risk_factors
        ),
    )

    verification = VerificationSection(
        verification_status=verification_report.status,
        total_cases=verification_report.total_cases,
        passed_cases=verification_report.passed_cases,
        failed_cases=verification_report.failed_cases,
        inconclusive_cases=verification_report.inconclusive_cases,
        skipped_cases=verification_report.skipped_cases,
        evidence=_text_tuple(verification_report.failure_evidence),
        case_results=tuple(
            {
                "case_id": result.case_id,
                "description": result.description,
                "status": result.status,
                "expected": _json_value(result.expected),
                "observed": _json_value(result.observed),
                "evidence": result.evidence,
                "required": result.required,
            }
            for result in verification_report.results
        ),
    )

    decision = DecisionSection(
        decision=decision_report.decision,
        accepted=bool(decision_report.accepted),
        verification_status=decision_report.verification_status,
        risk_level=decision_report.risk_level,
        risk_score=decision_report.risk_score,
        explanations=_text_tuple(decision_report.explanations),
    )

    recovery = RecoverySection(
        recovery_state=recovery_report.recovery_state,
        state_history=tuple(recovery_report.state_history),
        transitions=tuple(
            f"{transition.source_state} -> {transition.target_state}: "
            f"{transition.reason}"
            for transition in recovery_report.transitions
        ),
        initial_decision=recovery_report.initial_decision,
        initial_verification_status=recovery_report.initial_verification_status,
        rollback_attempted=recovery_report.rollback_attempted,
        rollback_succeeded=recovery_report.rollback_succeeded,
        rollback_verification_status=recovery_report.rollback_verification_status,
        final_verification_status=recovery_report.final_verification_status,
        final_candidate_id=recovery_report.final_candidate_id,
        replanning_required=recovery_report.replanning_required,
        migration_accepted=bool(recovery_report.accepted),
        replan_proposal_only=(
            recovery_report.replan_request is None
            or recovery_report.replan_request.status == "proposal"
        ),
        explanations=_text_tuple(recovery_report.explanations),
    )

    outcome = _final_outcome(decision_report.decision, recovery_report)

    return DashboardResult(
        migration_name=verification_report.migration_name,
        candidate_id=verification_report.candidate_id,
        final_outcome=outcome,
        final_outcome_detail=_OUTCOME_DETAIL[outcome],
        impact=impact,
        risk=risk,
        verification=verification,
        decision=decision,
        recovery=recovery,
        mode=mode,
        task_state=task_state,
        repository=(
            None
            if repository_evidence is None
            else RepositorySection.from_evidence(repository_evidence, rollback_mechanism)
        ),
    )
