"""Focused tests for the deterministic risk analysis engine."""

import json
from pathlib import Path

from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.risk_analyzer import (
    DIRECT_OLD_API_USAGE,
    FACTOR_ORDER,
    MIXED_API_STATE,
    MULTIPLE_IMPACTED_FILES,
    RENAMED_SYMBOLS,
    TEST_IMPACT,
    RiskAnalyzer,
)


def _migration(**overrides):
    values = {
        "name": "test-migration",
        "old_api": "LegacyDirectory.lookup(user_id)",
        "target_api": "ProfileDirectory.get_profile(user_id)",
        "old_symbols": ("LegacyDirectory", "lookup"),
        "target_symbols": ("ProfileDirectory", "get_profile"),
        "renamed_symbols": (),
        "affected_files": (),
    }
    values.update(overrides)
    return MigrationDescription(**values)


def _analyze(tmp_path, files, migration):
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    impact_report = ImpactAnalyzer(tmp_path).analyze(migration)
    return impact_report, RiskAnalyzer().analyze(migration, impact_report)


def test_no_evidence_is_low_risk(tmp_path):
    _, report = _analyze(tmp_path, {}, _migration())

    assert report.risk_level == "LOW"
    assert report.risk_score == 0
    assert report.risk_factors == ()
    assert report.explanations == ("No impact evidence was available for this migration.",)


def test_one_direct_old_reference_increases_risk(tmp_path):
    migration = _migration(old_symbols=("lookup",))
    _, report = _analyze(tmp_path, {"client.py": "lookup('user-001')\n"}, migration)

    assert report.risk_score == 2
    assert report.risk_factors[0].category == DIRECT_OLD_API_USAGE
    assert report.risk_factors[0].affected_symbols == ("lookup",)


def test_more_direct_old_references_increase_risk(tmp_path):
    migration = _migration(old_symbols=("lookup",))
    _, one = _analyze(tmp_path, {"client.py": "lookup('user-001')\n"}, migration)
    _, many = _analyze(
        tmp_path,
        {"client.py": "lookup('user-001')\nlookup('user-002')\nlookup('user-003')\n"},
        migration,
    )

    assert many.risk_score > one.risk_score
    assert many.direct_old_reference_count == 3


def test_renamed_symbols_increase_risk(tmp_path):
    migration = _migration(
        renamed_symbols=(("first_name", "given_name"),),
    )
    _, report = _analyze(tmp_path, {}, migration)

    factor = next(factor for factor in report.risk_factors if factor.category == RENAMED_SYMBOLS)
    assert factor.score == 3
    assert report.risk_score == 3


def test_affected_tests_increase_risk(tmp_path):
    migration = _migration(affected_files=("tests/test_profile.py",))
    _, report = _analyze(
        tmp_path,
        {"tests/test_profile.py": "Migration test notes.\n"},
        migration,
    )

    factor = next(factor for factor in report.risk_factors if factor.category == TEST_IMPACT)
    assert factor.score == 2
    assert report.affected_test_files == ("tests/test_profile.py",)


def test_multiple_impacted_files_increase_risk(tmp_path):
    migration = _migration(affected_files=("a.md", "b.md"))
    _, report = _analyze(
        tmp_path,
        {"a.md": "No symbols.\n", "b.md": "No symbols.\n"},
        migration,
    )

    factor = next(
        factor
        for factor in report.risk_factors
        if factor.category == MULTIPLE_IMPACTED_FILES
    )
    assert factor.score == 1
    assert report.impacted_file_count == 2


def test_mixed_old_and_target_references_are_reported(tmp_path):
    migration = _migration(old_symbols=("lookup",), target_symbols=("get_profile",))
    _, report = _analyze(
        tmp_path,
        {"mixed.py": "lookup('user-001')\nget_profile('user-001')\n"},
        migration,
    )

    factor = next(
        factor for factor in report.risk_factors if factor.category == MIXED_API_STATE
    )
    assert "Both OLD and TARGET" in factor.evidence
    assert "not an automatic regression" in factor.reason
    assert report.direct_old_reference_count == 1
    assert report.target_reference_count == 1


def test_risk_factors_are_deterministically_ordered(tmp_path):
    migration = _migration(
        old_symbols=("lookup",),
        target_symbols=("get_profile",),
        renamed_symbols=(("first_name", "given_name"),),
        affected_files=("tests/test_profile.py", "docs/notes.md"),
    )
    _, report = _analyze(
        tmp_path,
        {
            "client.py": "lookup\ngiven_name\n",
            "tests/test_profile.py": "get_profile\n",
            "docs/notes.md": "lookup\n",
        },
        migration,
    )

    assert tuple(factor.category for factor in report.risk_factors) == tuple(
        category for category in FACTOR_ORDER if any(
            factor.category == category for factor in report.risk_factors
        )
    )


def test_identical_input_produces_identical_report(tmp_path):
    migration = _migration(affected_files=("a.md", "b.md"))
    impact_report, first = _analyze(
        tmp_path,
        {"a.md": "lookup\n", "b.md": "get_profile\n"},
        migration,
    )
    second = RiskAnalyzer().analyze(migration, impact_report)

    assert first == second


def test_risk_analysis_does_not_modify_repository_contents(tmp_path):
    files = {
        "client.py": "LegacyDirectory.lookup\n",
        "tests/test_client.py": "ProfileDirectory.get_profile\n",
    }
    before = {}
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    migration = _migration()
    impact_report = ImpactAnalyzer(tmp_path).analyze(migration)
    RiskAnalyzer().analyze(migration, impact_report)

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_benchmark_metadata_integrates_with_risk_analyzer():
    repository_root = Path(__file__).resolve().parents[1]
    metadata = json.loads(
        (repository_root / "demo" / "migration_benchmark" / "migration_metadata.json").read_text(
            encoding="utf-8"
        )
    )
    migration = MigrationDescription.from_metadata(metadata)
    impact_report = ImpactAnalyzer(repository_root).analyze(migration)

    report = RiskAnalyzer().analyze(migration, impact_report)

    assert report.migration_name == "profile-label-directory"
    assert report.risk_score > 0
    assert report.direct_old_reference_count > 0
    assert report.target_reference_count > 0
    assert "demo/migration_benchmark/tests/test_old_baseline.py" in report.affected_test_files
    assert any(factor.category == DIRECT_OLD_API_USAGE for factor in report.risk_factors)
    assert any(factor.category == RENAMED_SYMBOLS for factor in report.risk_factors)


def test_explanations_are_generated_from_actual_evidence(tmp_path):
    migration = _migration(
        old_symbols=("lookup",),
        renamed_symbols=(("first_name", "given_name"),),
        affected_files=("tests/test_profile.py",),
    )
    _, report = _analyze(
        tmp_path,
        {
            "client.py": "lookup\n",
            "tests/test_profile.py": "No symbols.\n",
        },
        migration,
    )

    explanations = " ".join(report.explanations)
    assert "1 direct OLD API reference detected" in explanations
    assert "1 renamed field or symbol detected" in explanations
    assert "1 affected test file detected" in explanations
    assert "impacted" in explanations
