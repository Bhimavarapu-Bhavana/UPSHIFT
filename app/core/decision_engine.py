"""Deterministic migration decision layer over existing evidence reports.

The engine is a pure decision layer. It consumes already-produced reports and
never recomputes impact analysis, risk analysis, or verification. It does not
import those modules, does not touch the filesystem, and does not execute
candidate code, migrations, shell commands, or Git.

Correctness is decided only by independent verification evidence. Risk is
contextual evidence about migration complexity and never determines a
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence, Tuple

ACCEPT = "ACCEPT"
REJECT = "REJECT"
INCONCLUSIVE = "INCONCLUSIVE"

DECISIONS: Tuple[str, ...] = (ACCEPT, REJECT, INCONCLUSIVE)

VERIFICATION_PASS = "PASS"
VERIFICATION_FAIL = "FAIL"
VERIFICATION_INCONCLUSIVE = "INCONCLUSIVE"
VERIFICATION_STATUSES: Tuple[str, ...] = (
    VERIFICATION_PASS,
    VERIFICATION_FAIL,
    VERIFICATION_INCONCLUSIVE,
)

EVIDENCE_VERIFICATION = "verification"
EVIDENCE_RISK = "risk"
EVIDENCE_IMPACT = "impact"

ACCEPT_EXPLANATION = (
    "Verification passed all required cases; the migration candidate preserved "
    "the declared benchmark behavior."
)
REJECT_EXPLANATION = (
    "Verification failed one or more required cases; the migration candidate did "
    "not preserve the declared benchmark behavior."
)
INCONCLUSIVE_EXPLANATION = (
    "Verification did not provide sufficient required evidence to establish "
    "behavioral correctness."
)

IMPACT_ATTRIBUTES: Tuple[str, ...] = (
    "migration_name",
    "impacted_files",
    "direct_references",
    "target_references",
)
RISK_ATTRIBUTES: Tuple[str, ...] = (
    "migration_name",
    "risk_level",
    "risk_score",
)
VERIFICATION_ATTRIBUTES: Tuple[str, ...] = (
    "migration_name",
    "candidate_id",
    "status",
    "total_cases",
    "passed_cases",
    "failed_cases",
    "inconclusive_cases",
    "skipped_cases",
    "results",
)

__all__ = [
    "ACCEPT",
    "ACCEPT_EXPLANATION",
    "DECISIONS",
    "DecisionEvidence",
    "DecisionInputError",
    "DecisionReport",
    "INCONCLUSIVE",
    "INCONCLUSIVE_EXPLANATION",
    "MigrationDecisionEngine",
    "REJECT",
    "REJECT_EXPLANATION",
    "VERIFICATION_FAIL",
    "VERIFICATION_INCONCLUSIVE",
    "VERIFICATION_PASS",
    "VERIFICATION_STATUSES",
]


class DecisionInputError(ValueError):
    """Raised when a supplied report does not satisfy the decision interface."""


@dataclass(frozen=True)
class DecisionEvidence:
    """One categorized piece of evidence considered by the decision layer."""

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
class DecisionReport:
    """Immutable decision with the evidence that justified it."""

    migration_name: str
    decision: str
    verification_status: str
    risk_level: str
    risk_score: int
    total_cases: int
    passed_cases: int
    failed_cases: int
    inconclusive_cases: int
    skipped_cases: int
    candidate_id: str
    explanations: Tuple[str, ...]
    evidence: Tuple[DecisionEvidence, ...]

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}, got {self.decision!r}")
        if self.verification_status not in VERIFICATION_STATUSES:
            raise ValueError(
                "verification_status must be one of "
                f"{VERIFICATION_STATUSES}, got {self.verification_status!r}"
            )
        object.__setattr__(self, "explanations", tuple(self.explanations))
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def accepted(self) -> bool:
        return self.decision == ACCEPT

    def evidence_of_kind(self, kind: str) -> Tuple[DecisionEvidence, ...]:
        return tuple(item for item in self.evidence if item.kind == kind)


def _require_attributes(report: Any, attributes: Sequence[str], label: str) -> None:
    if report is None:
        raise DecisionInputError(f"{label} report is required")
    missing = [name for name in attributes if not hasattr(report, name)]
    if missing:
        raise DecisionInputError(
            f"{label} report is missing required attribute(s): {', '.join(missing)}"
        )


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionInputError(f"{label} must be an integer count")
    if value < 0:
        raise DecisionInputError(f"{label} must not be negative")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionInputError(f"{label} must be a non-empty string")
    return value.strip()


class MigrationDecisionEngine:
    """Combines impact, risk, and verification reports into one decision."""

    def decide(
        self,
        impact_report: Any,
        risk_report: Any,
        verification_report: Any,
    ) -> DecisionReport:
        """Return a deterministic decision from already-produced reports."""

        _require_attributes(impact_report, IMPACT_ATTRIBUTES, "impact")
        _require_attributes(risk_report, RISK_ATTRIBUTES, "risk")
        _require_attributes(verification_report, VERIFICATION_ATTRIBUTES, "verification")

        migration_name = self._shared_migration_name(
            impact_report, risk_report, verification_report
        )
        risk_level = _text(risk_report.risk_level, "risk_level")
        risk_score = _count(risk_report.risk_score, "risk_score")
        verification_status = _text(verification_report.status, "verification status")
        if verification_status not in VERIFICATION_STATUSES:
            raise DecisionInputError(
                "verification status must be one of "
                f"{VERIFICATION_STATUSES}, got {verification_status!r}"
            )
        candidate_id = _text(verification_report.candidate_id, "candidate_id")

        total_cases = _count(verification_report.total_cases, "total_cases")
        passed_cases = _count(verification_report.passed_cases, "passed_cases")
        failed_cases = _count(verification_report.failed_cases, "failed_cases")
        inconclusive_cases = _count(
            verification_report.inconclusive_cases, "inconclusive_cases"
        )
        skipped_cases = _count(verification_report.skipped_cases, "skipped_cases")

        failure_details = self._failure_details(verification_report.results)
        decision = self._decide(
            verification_status=verification_status,
            total_cases=total_cases,
            passed_cases=passed_cases,
            failed_cases=failed_cases,
            inconclusive_cases=inconclusive_cases,
            skipped_cases=skipped_cases,
        )

        impact_summary = self._impact_summary(impact_report)
        counts_summary = (
            f"Verification status {verification_status}: {passed_cases} passed, "
            f"{failed_cases} failed, {inconclusive_cases} inconclusive, "
            f"{skipped_cases} skipped across {total_cases} case(s)."
        )
        risk_summary = (
            f"Risk level: {risk_level} with score {risk_score} (complexity "
            "evidence only; it does not determine correctness)."
        )

        verification_evidence = DecisionEvidence(
            kind=EVIDENCE_VERIFICATION,
            summary=counts_summary,
            details=failure_details,
        )
        risk_evidence = DecisionEvidence(kind=EVIDENCE_RISK, summary=risk_summary)
        impact_evidence = DecisionEvidence(
            kind=EVIDENCE_IMPACT, summary=impact_summary
        )

        return DecisionReport(
            migration_name=migration_name,
            decision=decision,
            verification_status=verification_status,
            risk_level=risk_level,
            risk_score=risk_score,
            total_cases=total_cases,
            passed_cases=passed_cases,
            failed_cases=failed_cases,
            inconclusive_cases=inconclusive_cases,
            skipped_cases=skipped_cases,
            candidate_id=candidate_id,
            explanations=self._explanations(
                decision=decision,
                verification_status=verification_status,
                risk_level=risk_level,
                total_cases=total_cases,
                passed_cases=passed_cases,
                failed_cases=failed_cases,
                inconclusive_cases=inconclusive_cases,
                skipped_cases=skipped_cases,
                failure_details=failure_details,
            ),
            evidence=(
                verification_evidence,
                risk_evidence,
                impact_evidence,
            ),
        )

    @staticmethod
    def _shared_migration_name(*reports: Any) -> str:
        names = tuple(
            _text(report.migration_name, "migration_name") for report in reports
        )
        if len(set(names)) != 1:
            raise DecisionInputError(
                "all reports must describe the same migration, got: "
                + ", ".join(repr(name) for name in names)
            )
        return names[0]

    @staticmethod
    def _decide(
        *,
        verification_status: str,
        total_cases: int,
        passed_cases: int,
        failed_cases: int,
        inconclusive_cases: int,
        skipped_cases: int,
    ) -> str:
        required_evaluated = total_cases - skipped_cases
        if failed_cases or verification_status == VERIFICATION_FAIL:
            return REJECT
        if (
            verification_status == VERIFICATION_INCONCLUSIVE
            or inconclusive_cases
            or required_evaluated <= 0
            or passed_cases < required_evaluated
        ):
            return INCONCLUSIVE
        if verification_status == VERIFICATION_PASS and passed_cases >= 1:
            return ACCEPT
        return INCONCLUSIVE

    @staticmethod
    def _impact_summary(impact_report: Any) -> str:
        impacted = len(impact_report.impacted_files)
        direct = len(impact_report.direct_references)
        target = len(impact_report.target_references)
        return (
            f"Impact evidence: {impacted} impacted file(s), {direct} direct OLD "
            f"reference(s), {target} TARGET reference(s)."
        )

    @staticmethod
    def _failure_details(results: Iterable[Any]) -> Tuple[str, ...]:
        details = []
        for result in results:
            _require_attributes(
                result, ("case_id", "expected", "observed", "status"), "verification result"
            )
            if result.status != VERIFICATION_FAIL:
                continue
            details.append(
                f"Case {result.case_id} failed: expected {result.expected!r}, "
                f"observed {result.observed!r}."
            )
        return tuple(details)

    @staticmethod
    def _explanations(
        *,
        decision: str,
        verification_status: str,
        risk_level: str,
        total_cases: int,
        passed_cases: int,
        failed_cases: int,
        inconclusive_cases: int,
        skipped_cases: int,
        failure_details: Sequence[str],
    ) -> Tuple[str, ...]:
        if decision == ACCEPT:
            primary = ACCEPT_EXPLANATION
        elif decision == REJECT:
            primary = REJECT_EXPLANATION
        else:
            primary = INCONCLUSIVE_EXPLANATION

        independence = {
            ACCEPT: f"Verification passed independently of risk level {risk_level}.",
            REJECT: (
                f"The rejection comes from verification status "
                f"{verification_status}, not from risk level {risk_level}."
            ),
            INCONCLUSIVE: (
                f"The decision is inconclusive because required evidence is "
                f"missing, not because of risk level {risk_level}."
            ),
        }[decision]

        counts = (
            f"Verification status {verification_status}: {passed_cases} passed, "
            f"{failed_cases} failed, {inconclusive_cases} inconclusive, "
            f"{skipped_cases} skipped across {total_cases} case(s)."
        )
        risk_context = (
            f"Risk level: {risk_level} (complexity evidence only). "
            "Verification determines correctness."
        )
        return (primary, independence, counts, risk_context, *failure_details)
