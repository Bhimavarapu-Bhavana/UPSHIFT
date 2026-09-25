"""Focused tests for the independent verification engine."""

import json
from pathlib import Path

import pytest

from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.risk_analyzer import RiskAnalyzer
from app.verification.profile_label_benchmark import (
    BENCHMARK_ROOT,
    build_cases,
    load_candidate,
    load_metadata,
    verify_candidate,
)
from app.verification.verifier import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    SKIPPED,
    UNAVAILABLE,
    CandidateVerifier,
    UnknownCandidateError,
    VerificationCandidate,
    VerificationCase,
    format_case_evidence,
    verification_case_evidence,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_CASES = build_cases()
CASE_IDS = tuple(case.case_id for case in BENCHMARK_CASES)


def _migration(**overrides):
    values = {
        "name": "profile-label-directory",
        "old_api": "LegacyDirectory.lookup(user_id)",
        "target_api": "ProfileDirectory.get_profile(user_id)",
        "old_symbols": ("LegacyDirectory", "lookup"),
        "target_symbols": ("ProfileDirectory", "get_profile"),
        "renamed_symbols": (("first_name", "given_name"),),
        "affected_files": ("demo/migration_benchmark/tests/test_old_baseline.py",),
    }
    values.update(overrides)
    return MigrationDescription(**values)


def _stub_candidate(candidate_id, mapping, *, raises=None):
    def resolve(*inputs):
        if raises is not None:
            raise raises
        return mapping[inputs[0]]

    return VerificationCandidate(
        candidate_id=candidate_id, path=f"<stub:{candidate_id}>", resolve=resolve
    )


def _case(case_id, expected, *, description="stub case", inputs=("user-001",)):
    return VerificationCase(
        case_id=case_id, description=description, inputs=tuple(inputs), expected=expected
    )


def test_benchmark_defines_the_required_verification_cases():
    assert CASE_IDS == (
        "user-001",
        "user-002",
        "user-003",
        "empty-user",
        "missing-user",
    )
    assert all(case.expected for case in BENCHMARK_CASES)
    assert all(case.required for case in BENCHMARK_CASES)


def test_old_baseline_passes_its_own_preserved_behavior():
    report = verify_candidate("old_baseline")

    assert report.status == PASS
    assert report.total_cases == 5
    assert report.passed_cases == 5
    assert report.failed_cases == 0
    assert report.inconclusive_cases == 0
    assert report.skipped_cases == 0


def test_correct_migration_candidate_passes():
    report = verify_candidate("correct_migration")

    assert report.status == PASS
    assert report.passed_cases == 5
    assert report.failed_cases == 0
    assert report.candidate_id == "correct_migration"
    assert report.migration_name == "profile-label-directory"
    assert report.candidate_path == (
        "demo/migration_benchmark/candidates/correct_migration/profile_service.py"
    )


def test_regression_migration_candidate_fails():
    report = verify_candidate("regression_migration")

    assert report.status == FAIL
    assert report.total_cases == 5
    assert report.passed_cases == 4
    assert report.failed_cases == 1
    assert report.inconclusive_cases == 0


@pytest.mark.parametrize("case", BENCHMARK_CASES, ids=CASE_IDS)
def test_correct_candidate_matches_every_expected_label(case):
    report = verify_candidate("correct_migration")

    result = report.result_for(case.case_id)
    assert result.status == PASS
    assert result.expected == case.expected
    assert result.observed == case.expected
    assert result.failed is False


def test_user_002_catches_the_display_name_regression():
    report = verify_candidate("regression_migration")

    result = report.result_for("user-002")
    assert result.status == FAIL
    assert result.expected == "Amazing Grace"
    assert result.observed == "Grace Hopper"
    assert report.status == FAIL


@pytest.mark.parametrize("case", BENCHMARK_CASES, ids=CASE_IDS)
def test_failure_is_reported_for_its_own_case_only(case):
    report = verify_candidate("regression_migration")

    statuses = {result.case_id: result.status for result in report.results}
    if case.case_id == "user-002":
        assert statuses[case.case_id] == FAIL
    else:
        assert statuses[case.case_id] == PASS


def test_failure_evidence_contains_expected_and_observed_values():
    report = verify_candidate("regression_migration")

    result = report.result_for("user-002")
    assert "expected 'Amazing Grace'" in result.evidence
    assert "observed 'Grace Hopper'" in result.evidence
    assert report.failure_evidence == (result.evidence,)

    rendered = format_case_evidence(result)
    assert "case: user-002" in rendered
    assert "expected: 'Amazing Grace'" in rendered
    assert "observed: 'Grace Hopper'" in rendered
    assert "status: FAIL" in rendered
    assert "user-002" in verification_case_evidence(report.results)


def test_all_required_cases_passing_produces_pass():
    report = verify_candidate("correct_migration")

    assert report.status == PASS
    assert [result.status for result in report.results] == [PASS] * 5
    assert report.failure_evidence == ()


def test_any_required_case_failing_produces_fail():
    cases = (
        _case("passes", "Grace Hopper", inputs=("user-001",)),
        _case("fails", "Amazing Grace", inputs=("user-002",)),
        _case("also-passes", "Unknown user", inputs=("user-003",)),
    )
    candidate = _stub_candidate(
        "stub", {"user-001": "Grace Hopper", "user-002": "Unknown user"}
    )

    report = CandidateVerifier().verify(candidate, cases, migration_name="stub")

    assert report.status == FAIL
    assert report.passed_cases == 1
    assert report.failed_cases == 2
    assert report.result_for("fails").observed == "Unknown user"


def test_missing_evidence_never_becomes_pass():
    verifier = CandidateVerifier()

    no_cases = verifier.verify(
        _stub_candidate("stub", {}), (), migration_name="stub"
    )
    assert no_cases.status == INCONCLUSIVE
    assert no_cases.total_cases == 0
    assert no_cases.passed_cases == 0
    assert "no behavioral evidence" in no_cases.explanations[0]

    undefined_expected = verifier.verify(
        _stub_candidate("stub", {"user-001": "Ada Lovelace"}),
        (
            VerificationCase(
                case_id="user-001",
                description="Profile label for user-001",
                inputs=("user-001",),
            ),
        ),
        migration_name="stub",
    )
    assert undefined_expected.status == INCONCLUSIVE
    assert undefined_expected.inconclusive_cases == 1
    assert undefined_expected.passed_cases == 0
    assert undefined_expected.result_for("user-001").expected is UNAVAILABLE

    optional_only = verifier.verify(
        _stub_candidate("stub", {"user-001": "Ada Lovelace"}),
        (
            VerificationCase(
                case_id="optional",
                description="optional case",
                inputs=("user-001",),
                expected="Ada Lovelace",
                required=False,
            ),
        ),
        migration_name="stub",
    )
    assert optional_only.status == INCONCLUSIVE
    assert optional_only.skipped_cases == 1
    assert optional_only.result_for("optional").status == SKIPPED


def test_inconclusive_case_yields_inconclusive_report():
    cases = (
        _case("known", "Ada Lovelace"),
        VerificationCase(
            case_id="unknown", description="undefined case", inputs=("user-001",)
        ),
    )
    candidate = _stub_candidate("stub", {"user-001": "Ada Lovelace"})

    report = CandidateVerifier().verify(candidate, cases, migration_name="stub")

    assert report.status == INCONCLUSIVE
    assert report.passed_cases == 1
    assert report.inconclusive_cases == 1
    assert report.failed_cases == 0


def test_candidate_exception_is_recorded_as_failure_evidence():
    cases = (_case("boom", "Amazing Grace"),)
    candidate = _stub_candidate("stub", {}, raises=ValueError("directory unavailable"))

    report = CandidateVerifier().verify(candidate, cases, migration_name="stub")

    result = report.result_for("boom")
    assert report.status == FAIL
    assert result.status == FAIL
    assert result.observed is None
    assert "raised ValueError" in result.evidence


def test_observed_values_are_not_coerced_to_match():
    cases = (_case("typed", "Ada Lovelace"),)
    candidate = _stub_candidate("stub", {"user-001": 42})

    report = CandidateVerifier().verify(candidate, cases, migration_name="stub")

    assert report.status == FAIL
    assert report.result_for("typed").observed == 42


def test_only_declared_optional_cases_are_skipped():
    cases = (
        _case("required", "Ada Lovelace"),
        VerificationCase(
            case_id="optional",
            description="optional case",
            inputs=("user-001",),
            expected="never observed",
            required=False,
        ),
    )
    candidate = _stub_candidate("stub", {"user-001": "Ada Lovelace"})

    report = CandidateVerifier().verify(candidate, cases, migration_name="stub")

    assert report.status == PASS
    assert report.skipped_cases == 1
    assert report.result_for("optional").status == SKIPPED
    assert report.result_for("required").status == PASS


@pytest.mark.parametrize("candidate_id", ["old_baseline", "correct_migration", "regression_migration"])
def test_verification_is_deterministic(candidate_id):
    first = verify_candidate(candidate_id)
    second = verify_candidate(candidate_id)

    assert first == second
    assert tuple(result.case_id for result in first.results) == CASE_IDS
    assert tuple(result.observed for result in first.results) == tuple(
        result.observed for result in second.results
    )
    assert tuple(result.expected for result in first.results) == tuple(
        result.expected for result in second.results
    )
    assert tuple(result.status for result in first.results) == tuple(
        result.status for result in second.results
    )
    assert first.explanations == second.explanations


def test_verification_does_not_modify_candidate_or_benchmark_files():
    def snapshot():
        state = {}
        for path in sorted(BENCHMARK_ROOT.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            state[path.relative_to(BENCHMARK_ROOT).as_posix()] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
        return state

    load_candidate("correct_migration")
    before = snapshot()

    for candidate_id in ("old_baseline", "correct_migration", "regression_migration"):
        verify_candidate(candidate_id)

    assert snapshot() == before


def test_only_known_benchmark_candidates_can_be_loaded():
    for candidate_id in ("os", "demo.migration_benchmark.old.profile_service", ""):
        with pytest.raises(UnknownCandidateError):
            load_candidate(candidate_id)


def test_duplicate_case_identifiers_are_rejected():
    with pytest.raises(ValueError):
        CandidateVerifier().verify(
            _stub_candidate("stub", {"user-001": "x"}),
            (_case("duplicate", "a"), _case("duplicate", "b")),
            migration_name="stub",
        )


def test_engine_never_reads_or_depends_on_risk_analysis():
    source = (REPOSITORY_ROOT / "app" / "verification" / "verifier.py").read_text(
        encoding="utf-8"
    )
    assert "risk_analyzer" not in source
    assert "app.core" not in source

    import app.verification.verifier as verifier_module

    assert not hasattr(verifier_module, "RiskReport")
    assert not hasattr(verifier_module, "RiskAnalyzer")


def test_impact_risk_and_verification_reports_coexist_without_coupling():
    metadata = json.loads(
        (REPOSITORY_ROOT / "demo" / "migration_benchmark" / "migration_metadata.json").read_text(
            encoding="utf-8"
        )
    )
    migration = MigrationDescription.from_metadata(metadata)
    impact_report = ImpactAnalyzer(REPOSITORY_ROOT).analyze(migration)
    risk_report = RiskAnalyzer().analyze(migration, impact_report)
    verification_report = verify_candidate("correct_migration")

    assert impact_report.old_api == risk_report.old_api
    assert verification_report.migration_name == risk_report.migration_name
    assert risk_report.risk_score > 0
    assert verification_report.status == PASS
    assert not hasattr(verification_report, "risk_score")
    assert "risk" not in {field for field in verification_report.__dataclass_fields__}


def test_low_risk_migration_can_still_fail_verification(tmp_path):
    migration = MigrationDescription(
        name="low-risk-migration",
        old_api="LegacyDirectory.lookup(user_id)",
        target_api="ProfileDirectory.get_profile(user_id)",
    )
    impact_report = ImpactAnalyzer(tmp_path).analyze(migration)
    risk_report = RiskAnalyzer().analyze(migration, impact_report)

    assert risk_report.risk_score == 0
    assert risk_report.risk_level == "LOW"

    report = verify_candidate("regression_migration")

    assert report.status == FAIL
    assert risk_report.risk_level == "LOW"


def test_high_risk_migration_can_still_pass_verification():
    metadata = json.loads(
        (REPOSITORY_ROOT / "demo" / "migration_benchmark" / "migration_metadata.json").read_text(
            encoding="utf-8"
        )
    )
    migration = MigrationDescription.from_metadata(metadata)
    risk_report = RiskAnalyzer().analyze(
        migration, ImpactAnalyzer(REPOSITORY_ROOT).analyze(migration)
    )

    assert risk_report.risk_level == "HIGH"
    assert verify_candidate("correct_migration").status == PASS


def test_expected_values_come_from_benchmark_metadata_not_the_engine():
    metadata = load_metadata()
    preserved = tuple(
        (entry["user_id"], entry["expected_label"]) for entry in metadata["preserved_behavior"]
    )
    from_engine = tuple((case.case_id, case.expected) for case in build_cases())

    assert from_engine == preserved

    engine_source = (REPOSITORY_ROOT / "app" / "verification" / "verifier.py").read_text(
        encoding="utf-8"
    )
    for expected_label in (value for _, value in preserved):
        assert f'"{expected_label}"' not in engine_source
        assert f"'{expected_label}'" not in engine_source
