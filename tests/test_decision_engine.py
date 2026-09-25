"""Focused tests for the deterministic migration decision layer."""

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

import pytest

from app.core.decision_engine import (
    ACCEPT,
    ACCEPT_EXPLANATION,
    EVIDENCE_IMPACT,
    EVIDENCE_RISK,
    EVIDENCE_VERIFICATION,
    INCONCLUSIVE,
    INCONCLUSIVE_EXPLANATION,
    REJECT,
    REJECT_EXPLANATION,
    DecisionInputError,
    DecisionReport,
    MigrationDecisionEngine,
)
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.risk_analyzer import RiskAnalyzer
from app.verification.profile_label_benchmark import (
    BENCHMARK_ROOT,
    build_cases,
    verify_candidate,
)
from app.verification.verifier import (
    CandidateVerifier,
    VerificationCandidate,
    VerificationCase,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = "profile-label-directory"


@dataclass(frozen=True)
class _StubImpact:
    migration_name: str = MIGRATION
    impacted_files: Tuple[str, ...] = ()
    direct_references: Tuple[str, ...] = ()
    target_references: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _StubRisk:
    migration_name: str = MIGRATION
    risk_level: str = "LOW"
    risk_score: int = 0


@dataclass(frozen=True)
class _StubResult:
    case_id: str
    expected: Any
    observed: Any
    status: str


@dataclass(frozen=True)
class _StubVerification:
    migration_name: str
    candidate_id: str
    status: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    inconclusive_cases: int
    skipped_cases: int
    results: Tuple[_StubResult, ...] = ()


def _verification(
    *,
    status: str,
    passed: int = 0,
    failed: int = 0,
    inconclusive: int = 0,
    skipped: int = 0,
    results: Tuple[_StubResult, ...] = (),
    migration_name: str = MIGRATION,
    candidate_id: str = "stub-candidate",
) -> _StubVerification:
    return _StubVerification(
        migration_name=migration_name,
        candidate_id=candidate_id,
        status=status,
        total_cases=passed + failed + inconclusive + skipped,
        passed_cases=passed,
        failed_cases=failed,
        inconclusive_cases=inconclusive,
        skipped_cases=skipped,
        results=results,
    )


def _decide(verification=None, *, risk=None, impact=None):
    return MigrationDecisionEngine().decide(
        _StubImpact() if impact is None else impact,
        risk if risk is not None else _StubRisk(),
        verification,
    )


def _passing_report():
    return _verification(
        status="PASS",
        passed=5,
        results=tuple(
            _StubResult(case_id=case_id, expected="value", observed="value", status="PASS")
            for case_id in ("a", "b", "c", "d", "e")
        ),
    )


def _failing_report(migration_name: str = MIGRATION):
    return _verification(
        status="FAIL",
        passed=4,
        failed=1,
        migration_name=migration_name,
        results=(
            _StubResult(
                case_id="user-002",
                expected="Amazing Grace",
                observed="Grace Hopper",
                status="FAIL",
            ),
        ),
    )


def _benchmark_reports(candidate_id):
    metadata = json.loads(
        (BENCHMARK_ROOT / "migration_metadata.json").read_text(encoding="utf-8")
    )
    migration = MigrationDescription.from_metadata(metadata)
    impact_report = ImpactAnalyzer(REPOSITORY_ROOT).analyze(migration)
    risk_report = RiskAnalyzer().analyze(migration, impact_report)
    verification_report = verify_candidate(candidate_id)
    return impact_report, risk_report, verification_report


def test_accept_when_all_required_cases_pass():
    report = _decide(_passing_report())

    assert report.decision == ACCEPT
    assert report.accepted is True
    assert report.verification_status == "PASS"
    assert report.passed_cases == 5
    assert report.failed_cases == 0


def test_reject_when_verification_fails():
    report = _decide(_failing_report())

    assert report.decision == REJECT
    assert report.accepted is False
    assert report.verification_status == "FAIL"
    assert report.failed_cases == 1
    assert "user-002" in " ".join(report.explanations)
    assert "expected 'Amazing Grace', observed 'Grace Hopper'" in " ".join(
        report.explanations
    )


def test_inconclusive_when_verification_is_inconclusive():
    report = _decide(_verification(status="INCONCLUSIVE", inconclusive=1))

    assert report.decision == INCONCLUSIVE
    assert report.accepted is False


def test_high_risk_with_passing_verification_is_accepted():
    report = _decide(_passing_report(), risk=_StubRisk(risk_level="HIGH", risk_score=241))

    assert report.decision == ACCEPT
    assert report.risk_level == "HIGH"
    assert report.risk_score == 241
    assert report.explanations[0] == ACCEPT_EXPLANATION
    assert "risk level HIGH" in report.explanations[1]
    assert "complexity evidence only" in report.explanations[3]


def test_low_risk_with_failing_verification_is_rejected():
    report = _decide(_failing_report(), risk=_StubRisk(risk_level="LOW", risk_score=0))

    assert report.decision == REJECT
    assert report.risk_level == "LOW"
    assert report.risk_score == 0
    assert report.explanations[0] == REJECT_EXPLANATION
    assert "not from risk level LOW" in report.explanations[1]


@pytest.mark.parametrize("risk_level", ["LOW", "MEDIUM", "HIGH"])
def test_risk_level_never_changes_the_decision(risk_level):
    passing = _decide(_passing_report(), risk=_StubRisk(risk_level=risk_level, risk_score=99))
    failing = _decide(_failing_report(), risk=_StubRisk(risk_level=risk_level, risk_score=99))

    assert passing.decision == ACCEPT
    assert failing.decision == REJECT


def test_risk_never_accepts_failing_or_missing_evidence():
    risky = _StubRisk(risk_level="HIGH", risk_score=999)

    assert _decide(_failing_report(), risk=risky).decision == REJECT
    assert (
        _decide(_verification(status="INCONCLUSIVE"), risk=risky).decision
        == INCONCLUSIVE
    )
    assert _decide(_verification(status="PASS"), risk=risky).decision == INCONCLUSIVE


def test_no_verification_cases_is_inconclusive():
    report = _decide(_verification(status="PASS", skipped=0))

    assert report.decision == INCONCLUSIVE
    assert report.total_cases == 0
    assert report.explanations[0] == INCONCLUSIVE_EXPLANATION


def test_required_case_without_expected_behavior_is_inconclusive():
    verification_report = CandidateVerifier().verify(
        VerificationCandidate(
            candidate_id="stub",
            path="<stub>",
            resolve=lambda *inputs: "Ada Lovelace",
        ),
        (
            VerificationCase(
                case_id="user-001", description="Profile label", inputs=("user-001",)
            ),
        ),
        migration_name=MIGRATION,
    )

    report = _decide(verification_report)

    assert verification_report.status == "INCONCLUSIVE"
    assert report.decision == INCONCLUSIVE
    assert report.inconclusive_cases == 1
    assert report.passed_cases == 0


def test_skipped_only_evidence_is_inconclusive():
    verification_report = CandidateVerifier().verify(
        VerificationCandidate(
            candidate_id="stub",
            path="<stub>",
            resolve=lambda *inputs: "Ada Lovelace",
        ),
        (
            VerificationCase(
                case_id="optional",
                description="optional probe",
                inputs=("user-001",),
                expected="Ada Lovelace",
                required=False,
            ),
        ),
        migration_name=MIGRATION,
    )

    report = _decide(verification_report)

    assert verification_report.status == "INCONCLUSIVE"
    assert verification_report.skipped_cases == 1
    assert report.decision == INCONCLUSIVE
    assert report.skipped_cases == 1


def test_required_failure_is_rejected_even_with_matching_pass_count():
    report = _decide(_verification(status="PASS", passed=3, failed=1))

    assert report.decision == REJECT


def test_failed_case_takes_precedence_over_inconclusive_status():
    report = _decide(_verification(status="INCONCLUSIVE", failed=1))

    assert report.decision == REJECT


def test_explanations_match_the_decision():
    accept = _decide(_passing_report(), risk=_StubRisk(risk_level="HIGH", risk_score=12))
    reject = _decide(_failing_report(), risk=_StubRisk(risk_level="LOW", risk_score=1))
    unknown = _decide(_verification(status="INCONCLUSIVE"), risk=_StubRisk(risk_level="MEDIUM", risk_score=7))

    assert accept.explanations[0] == (
        "Verification passed all required cases; the migration candidate preserved "
        "the declared benchmark behavior."
    )
    assert reject.explanations[0] == (
        "Verification failed one or more required cases; the migration candidate did "
        "not preserve the declared benchmark behavior."
    )
    assert unknown.explanations[0] == (
        "Verification did not provide sufficient required evidence to establish "
        "behavioral correctness."
    )
    assert all(isinstance(line, str) and line for line in reject.explanations)


def test_risk_is_presented_only_as_context():
    report = _decide(_failing_report(), risk=_StubRisk(risk_level="HIGH", risk_score=241))

    text = " ".join(report.explanations)
    assert "complexity evidence only" in text
    assert "Verification determines correctness" in text
    assert "not from risk level HIGH" in text
    assert "risk caused" not in text.lower()
    assert "high risk caused" not in text.lower()

    risk_evidence = report.evidence_of_kind(EVIDENCE_RISK)
    assert len(risk_evidence) == 1
    assert risk_evidence[0].summary.startswith("Risk level: HIGH")
    assert "does not determine correctness" in risk_evidence[0].summary


def test_report_evidence_is_grouped_by_kind():
    report = _decide(_failing_report())

    assert [item.kind for item in report.evidence] == [
        EVIDENCE_VERIFICATION,
        EVIDENCE_RISK,
        EVIDENCE_IMPACT,
    ]
    verification = report.evidence_of_kind(EVIDENCE_VERIFICATION)
    assert len(verification) == 1
    assert verification[0].details == (
        "Case user-002 failed: expected 'Amazing Grace', observed 'Grace Hopper'.",
    )
    assert report.evidence_of_kind(EVIDENCE_IMPACT)[0].summary.startswith(
        "Impact evidence:"
    )


def test_decision_is_deterministic():
    impact_report, risk_report, verification_report = _benchmark_reports("correct_migration")

    first = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )
    second = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )

    assert first == second
    assert first.explanations == second.explanations
    assert first.evidence == second.evidence


def test_report_is_immutable_and_tuple_based():
    report = _decide(_passing_report())

    assert dataclasses.is_dataclass(report)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.decision = REJECT
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.evidence[0].summary = "changed"
    assert isinstance(report.explanations, tuple)
    assert isinstance(report.evidence, tuple)
    assert isinstance(report.evidence[0].details, tuple)


def test_report_keeps_only_decision_relevant_fields():
    expected_fields = {
        "migration_name",
        "decision",
        "verification_status",
        "risk_level",
        "risk_score",
        "total_cases",
        "passed_cases",
        "failed_cases",
        "inconclusive_cases",
        "skipped_cases",
        "candidate_id",
        "explanations",
        "evidence",
    }

    assert set(DecisionReport.__dataclass_fields__) == expected_fields


def test_benchmark_decisions_match_expected_outcomes():
    expected = {
        "old_baseline": ACCEPT,
        "correct_migration": ACCEPT,
        "regression_migration": REJECT,
    }

    for candidate_id, decision in expected.items():
        impact_report, risk_report, verification_report = _benchmark_reports(candidate_id)
        report = MigrationDecisionEngine().decide(
            impact_report, risk_report, verification_report
        )
        assert report.decision == decision, candidate_id
        assert report.migration_name == MIGRATION
        assert report.candidate_id == candidate_id


def test_benchmark_candidates_agree_on_risk_but_not_on_decision():
    _, correct_risk, correct_verification = _benchmark_reports("correct_migration")
    _, regression_risk, regression_verification = _benchmark_reports(
        "regression_migration"
    )

    assert correct_risk.risk_level == regression_risk.risk_level
    assert correct_risk.risk_score == regression_risk.risk_score
    assert correct_verification.status == "PASS"
    assert regression_verification.status == "FAIL"

    accept = _decide(correct_verification, risk=correct_risk)
    reject = _decide(regression_verification, risk=regression_risk)

    assert accept.decision == ACCEPT
    assert reject.decision == REJECT


def test_impact_risk_and_verification_reports_produce_one_decision():
    for candidate_id, decision in (
        ("correct_migration", ACCEPT),
        ("regression_migration", REJECT),
    ):
        impact_report, risk_report, verification_report = _benchmark_reports(candidate_id)
        assert impact_report.migration_name == risk_report.migration_name
        assert verification_report.migration_name == risk_report.migration_name

        report = MigrationDecisionEngine().decide(
            impact_report, risk_report, verification_report
        )

        assert report.decision == decision
        assert report.risk_level == risk_report.risk_level
        assert report.risk_score == risk_report.risk_score
        assert not hasattr(report, "impacted_files")
        assert not hasattr(report, "risk_factors")
        assert not hasattr(report, "results")


def test_low_risk_migration_with_failing_verification_is_rejected(tmp_path):
    migration = MigrationDescription(
        name="low-risk-migration",
        old_api="LegacyDirectory.lookup(user_id)",
        target_api="ProfileDirectory.get_profile(user_id)",
    )
    impact_report = ImpactAnalyzer(tmp_path).analyze(migration)
    risk_report = RiskAnalyzer().analyze(migration, impact_report)
    verification_report = _failing_report(migration_name="low-risk-migration")

    report = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )

    assert risk_report.risk_level == "LOW"
    assert risk_report.risk_score == 0
    assert report.decision == REJECT


def test_high_risk_migration_with_passing_verification_is_accepted():
    impact_report, risk_report, verification_report = _benchmark_reports(
        "correct_migration"
    )
    assert risk_report.risk_level == "HIGH"

    report = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )

    assert report.decision == ACCEPT


def test_decision_does_not_modify_any_files():
    def snapshot():
        state = {}
        for path in sorted(REPOSITORY_ROOT.rglob("*")):
            relative = path.relative_to(REPOSITORY_ROOT)
            if not path.is_file() or "__pycache__" in relative.parts:
                continue
            if relative.parts[:2] == (".git",) or relative.parts[0] == ".venv":
                continue
            state[relative.as_posix()] = (path.read_bytes(), path.stat().st_mtime_ns)
        return state

    verify_candidate("correct_migration")
    before = snapshot()

    for candidate_id in ("old_baseline", "correct_migration", "regression_migration"):
        MigrationDecisionEngine().decide(*_benchmark_reports(candidate_id))

    assert snapshot() == before


def test_engine_does_not_import_analysis_or_verification_modules():
    source = (REPOSITORY_ROOT / "app" / "core" / "decision_engine.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "impact_analyzer",
        "risk_analyzer",
        "app.verification",
        "CandidateVerifier",
        "ImpactAnalyzer",
        "RiskAnalyzer",
        "subprocess",
        "os.system",
    ):
        assert forbidden not in source

    import app.core.decision_engine as engine_module

    for forbidden in (
        "ImpactAnalyzer",
        "RiskAnalyzer",
        "CandidateVerifier",
        "ImpactReport",
        "RiskReport",
        "VerificationReport",
    ):
        assert not hasattr(engine_module, forbidden)


def test_mismatched_migration_reports_are_rejected():
    with pytest.raises(DecisionInputError):
        _decide(_passing_report(), risk=_StubRisk(migration_name="other-migration"))


def test_unknown_verification_status_is_rejected():
    with pytest.raises(DecisionInputError):
        _decide(_verification(status="MAYBE", passed=1))


def test_inconsistent_counts_are_rejected():
    with pytest.raises(DecisionInputError):
        _decide(_verification(status="PASS", passed=2, failed=1, skipped=-1))
    with pytest.raises(DecisionInputError):
        _decide(_verification(status="PASS", passed=1), risk=_StubRisk(risk_score=-3))


def test_missing_report_attributes_are_rejected():
    @dataclass(frozen=True)
    class _Incomplete:
        migration_name: str = MIGRATION

    with pytest.raises(DecisionInputError):
        _decide(_Incomplete())


def test_benchmark_cases_are_not_hard_coded_in_the_engine():
    source = (REPOSITORY_ROOT / "app" / "core" / "decision_engine.py").read_text(
        encoding="utf-8"
    )

    for case in build_cases():
        assert f'"{case.expected}"' not in source
        assert f"'{case.expected}'" not in source
        assert f'"{case.case_id}"' not in source
