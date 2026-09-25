"""Deterministic, evidence-based verification of a migration candidate.

The engine is independent from risk analysis. It never reads a ``RiskReport``
and never converts a risk score into a correctness conclusion. Correctness is
decided only by comparing candidate behavior against explicitly declared
expected behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence, Tuple

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
SKIPPED = "SKIPPED"

RESULT_STATUSES: Tuple[str, ...] = (PASS, FAIL, INCONCLUSIVE, SKIPPED)
REPORT_STATUSES: Tuple[str, ...] = (PASS, FAIL, INCONCLUSIVE)

__all__ = [
    "CandidateVerifier",
    "FAIL",
    "INCONCLUSIVE",
    "PASS",
    "REPORT_STATUSES",
    "RESULT_STATUSES",
    "SKIPPED",
    "UNAVAILABLE",
    "UnknownCandidateError",
    "VerificationCandidate",
    "VerificationCase",
    "VerificationReport",
    "VerificationResult",
    "format_case_evidence",
    "verification_case_evidence",
]


class UnknownCandidateError(ValueError):
    """Raised when a candidate identifier is not part of the known benchmark."""


class _Unavailable:
    """Sentinel type for expected behavior the benchmark does not define."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNAVAILABLE"

    def __str__(self) -> str:
        return "UNAVAILABLE"


UNAVAILABLE = _Unavailable()


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class VerificationCase:
    """One behavioral case with explicitly declared expected behavior."""

    case_id: str
    description: str
    inputs: Tuple[Any, ...] = ()
    expected: Any = UNAVAILABLE
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _text(self.case_id, "case_id"))
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "required", bool(self.required))


@dataclass(frozen=True)
class VerificationCandidate:
    """A controlled, already-loaded behavior provider under verification.

    ``resolve`` must be a narrow, in-process callable supplied by the caller.
    The engine never builds one from user-supplied source, file paths, shell
    commands, or repository configuration.
    """

    candidate_id: str
    path: str
    resolve: Callable[..., Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "path", _text(self.path, "path"))
        if not callable(self.resolve):
            raise ValueError("resolve must be callable")


@dataclass(frozen=True)
class VerificationResult:
    """Outcome and evidence for a single verification case."""

    case_id: str
    description: str
    expected: Any
    observed: Any
    status: str
    evidence: str
    required: bool = True

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValueError(
                f"status must be one of {RESULT_STATUSES}, got {self.status!r}"
            )
        object.__setattr__(self, "case_id", _text(self.case_id, "case_id"))
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "evidence", _text(self.evidence, "evidence"))

    @property
    def passed(self) -> bool:
        return self.status == PASS

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass(frozen=True)
class VerificationReport:
    """Deterministic verification outcome for one candidate and case set."""

    migration_name: str
    candidate_id: str
    candidate_path: str
    status: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    inconclusive_cases: int
    skipped_cases: int
    results: Tuple[VerificationResult, ...]
    explanations: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in REPORT_STATUSES:
            raise ValueError(
                f"status must be one of {REPORT_STATUSES}, got {self.status!r}"
            )
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "explanations", tuple(self.explanations))

    def result_for(self, case_id: str) -> VerificationResult:
        """Return the result recorded for ``case_id``."""

        for result in self.results:
            if result.case_id == case_id:
                return result
        raise KeyError(case_id)

    @property
    def failure_evidence(self) -> Tuple[str, ...]:
        """Evidence strings for every failing case, in case order."""

        return tuple(result.evidence for result in self.results if result.failed)


def _matches(observed: Any, expected: Any) -> bool:
    """Compare observed and expected values without implicit coercion."""

    if type(observed) is not type(expected):
        return False
    return bool(observed == expected)


def format_case_evidence(result: VerificationResult) -> str:
    """Render one result in the stable, human-readable evidence format."""

    return (
        f"case: {result.case_id}\n"
        f"description: {result.description}\n"
        f"expected: {result.expected!r}\n"
        f"observed: {result.observed!r}\n"
        f"status: {result.status}\n"
        f"evidence: {result.evidence}"
    )


class CandidateVerifier:
    """Compares candidate behavior against declared expected behavior."""

    def verify(
        self,
        candidate: VerificationCandidate,
        cases: Iterable[VerificationCase],
        *,
        migration_name: str = "unknown-migration",
    ) -> VerificationReport:
        """Return an evidence-based report. Never inspects a risk score."""

        if not isinstance(candidate, VerificationCandidate):
            raise TypeError("candidate must be a VerificationCandidate")

        ordered_cases = self._validated_cases(cases)
        results = tuple(
            self._verify_case(candidate, case) for case in ordered_cases
        )

        passed = sum(1 for result in results if result.passed)
        failed = sum(1 for result in results if result.failed)
        inconclusive = sum(
            1 for result in results if result.status == INCONCLUSIVE
        )
        skipped = sum(1 for result in results if result.status == SKIPPED)

        return VerificationReport(
            migration_name=_text(migration_name, "migration_name"),
            candidate_id=candidate.candidate_id,
            candidate_path=candidate.path,
            status=self._report_status(results),
            total_cases=len(results),
            passed_cases=passed,
            failed_cases=failed,
            inconclusive_cases=inconclusive,
            skipped_cases=skipped,
            results=results,
            explanations=self._explanations(
                candidate, results, passed, failed, inconclusive, skipped
            ),
        )

    @staticmethod
    def _validated_cases(cases: Iterable[VerificationCase]) -> Tuple[VerificationCase, ...]:
        ordered: list[VerificationCase] = []
        seen = set()
        for case in cases:
            if not isinstance(case, VerificationCase):
                raise TypeError("every case must be a VerificationCase")
            if case.case_id in seen:
                raise ValueError(f"duplicate case_id: {case.case_id!r}")
            seen.add(case.case_id)
            ordered.append(case)
        return tuple(ordered)

    @staticmethod
    def _report_status(results: Sequence[VerificationResult]) -> str:
        evaluated = [result for result in results if result.status != SKIPPED]
        if any(result.status == FAIL for result in results):
            return FAIL
        if not evaluated or any(result.status == INCONCLUSIVE for result in results):
            return INCONCLUSIVE
        return PASS

    @staticmethod
    def _verify_case(
        candidate: VerificationCandidate, case: VerificationCase
    ) -> VerificationResult:
        if not case.required:
            return VerificationResult(
                case_id=case.case_id,
                description=case.description,
                expected=case.expected,
                observed=UNAVAILABLE,
                status=SKIPPED,
                evidence="Optional case is not required for this verification.",
                required=False,
            )

        if case.expected is UNAVAILABLE:
            return VerificationResult(
                case_id=case.case_id,
                description=case.description,
                expected=UNAVAILABLE,
                observed=UNAVAILABLE,
                status=INCONCLUSIVE,
                evidence=(
                    "No expected behavior is defined for this case, so the "
                    "candidate cannot be judged."
                ),
            )

        try:
            observed = candidate.resolve(*case.inputs)
        except Exception as error:
            return VerificationResult(
                case_id=case.case_id,
                description=case.description,
                expected=case.expected,
                observed=None,
                status=FAIL,
                evidence=(
                    f"Candidate raised {type(error).__name__} while resolving the "
                    f"case, so expected {case.expected!r} was not produced."
                ),
            )

        if _matches(observed, case.expected):
            return VerificationResult(
                case_id=case.case_id,
                description=case.description,
                expected=case.expected,
                observed=observed,
                status=PASS,
                evidence=(
                    f"Expected {case.expected!r} and observed {observed!r} match."
                ),
            )

        return VerificationResult(
            case_id=case.case_id,
            description=case.description,
            expected=case.expected,
            observed=observed,
            status=FAIL,
            evidence=(
                f"Behavior mismatch: expected {case.expected!r} but observed "
                f"{observed!r}."
            ),
        )

    @staticmethod
    def _explanations(
        candidate: VerificationCandidate,
        results: Sequence[VerificationResult],
        passed: int,
        failed: int,
        inconclusive: int,
        skipped: int,
    ) -> Tuple[str, ...]:
        required_total = passed + failed + inconclusive
        if required_total == 0:
            summary = (
                f"No required verification cases were evaluated for candidate "
                f"{candidate.candidate_id!r}, so no behavioral evidence was "
                "available."
            )
        else:
            summary = (
                f"Candidate {candidate.candidate_id!r} passed {passed} of "
                f"{required_total} required verification case(s)."
            )

        details = [result.evidence for result in results if result.required]
        notes: list[str] = [summary, *details]
        if skipped:
            notes.append(f"{skipped} optional case(s) were skipped.")
        return tuple(notes)


def verification_case_evidence(results: Iterable[VerificationResult]) -> str:
    """Render every result in order, for logs and documentation examples."""

    return "\n".join(format_case_evidence(result) for result in results)
