"""Focused unit and integration tests for the impact analyzer."""

import json
from pathlib import Path

import pytest

from app.core.impact_analyzer import (
    ImpactAnalyzer,
    MigrationDescription,
    UnsafeRepositoryPathError,
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


def test_detects_old_api_references(tmp_path):
    (tmp_path / "legacy_client.py").write_text(
        "from legacy import LegacyDirectory\nvalue = directory.lookup('user-001')\n",
        encoding="utf-8",
    )

    report = ImpactAnalyzer(tmp_path).analyze(_migration())

    assert {impact.path for impact in report.impacted_files} == {"legacy_client.py"}
    assert {reference.symbol for reference in report.direct_references} == {
        "LegacyDirectory",
        "lookup",
    }
    assert report.target_references == ()


def test_detects_target_api_references(tmp_path):
    (tmp_path / "target_client.py").write_text(
        "from target import ProfileDirectory\nvalue = directory.get_profile('user-001')\n",
        encoding="utf-8",
    )

    report = ImpactAnalyzer(tmp_path).analyze(_migration())

    assert {reference.symbol for reference in report.target_references} == {
        "ProfileDirectory",
        "get_profile",
    }
    assert report.direct_references == ()


def test_detects_renamed_field_references(tmp_path):
    (tmp_path / "old_profile.py").write_text(
        "profile.first_name\nprofile.last_name\nprofile.preferred_name\n",
        encoding="utf-8",
    )
    (tmp_path / "target_profile.py").write_text(
        "profile.given_name\nprofile.family_name\nprofile.display_name\n",
        encoding="utf-8",
    )
    migration = _migration(
        old_symbols=("first_name", "last_name", "preferred_name"),
        target_symbols=("given_name", "family_name", "display_name"),
        renamed_symbols=(
            ("first_name", "given_name"),
            ("last_name", "family_name"),
            ("preferred_name", "display_name"),
        ),
    )

    report = ImpactAnalyzer(tmp_path).analyze(migration)

    old_impact = next(
        impact for impact in report.impacted_files if impact.path == "old_profile.py"
    )
    target_impact = next(
        impact for impact in report.impacted_files if impact.path == "target_profile.py"
    )
    assert old_impact.old_symbols == ("first_name", "last_name", "preferred_name")
    assert target_impact.target_symbols == ("display_name", "family_name", "given_name")
    assert any("renamed to 'given_name'" in reason for reason in old_impact.reasons)
    assert any("renamed from 'preferred_name'" in reason for reason in target_impact.reasons)


def test_includes_metadata_listed_files_without_symbols(tmp_path):
    (tmp_path / "docs").mkdir()
    path = tmp_path / "docs" / "migration_notes.md"
    path.write_text("Migration notes without code symbols.\n", encoding="utf-8")
    migration = _migration(affected_files=("docs/migration_notes.md",))

    report = ImpactAnalyzer(tmp_path).analyze(migration)

    assert report.metadata_listed_files == ("docs/migration_notes.md",)
    impact = report.impacted_files[0]
    assert impact.path == "docs/migration_notes.md"
    assert impact.metadata_listed is True
    assert impact.reasons == (
        "potentially impacted: file is listed in migration metadata as affected",
    )


def test_results_are_deterministically_sorted(tmp_path):
    (tmp_path / "z_source.py").write_text("LegacyDirectory.lookup\n", encoding="utf-8")
    (tmp_path / "a_source.py").write_text("lookup('user-001')\n", encoding="utf-8")
    migration = _migration(affected_files=("z_metadata.md", "a_metadata.md"))
    analyzer = ImpactAnalyzer(tmp_path)

    first = analyzer.analyze(migration)
    second = analyzer.analyze(migration)

    assert first == second
    assert first.metadata_listed_files == ("a_metadata.md", "z_metadata.md")
    assert [impact.path for impact in first.impacted_files] == sorted(
        impact.path for impact in first.impacted_files
    )
    assert [reference.path for reference in first.direct_references] == sorted(
        reference.path for reference in first.direct_references
    )


def test_ignores_excluded_directories_and_secret_files(tmp_path):
    excluded_directories = (
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "build",
        "dist",
        "secrets",
    )
    for directory_name in excluded_directories:
        directory = tmp_path / directory_name
        directory.mkdir()
        (directory / "leak.py").write_text("LegacyDirectory.lookup\n", encoding="utf-8")

    (tmp_path / ".env").write_text("TOKEN=do-not-scan\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("LegacyDirectory.lookup\n", encoding="utf-8")

    report = ImpactAnalyzer(tmp_path).analyze(_migration())

    assert {impact.path for impact in report.impacted_files} == {"kept.py"}


def test_analysis_does_not_modify_repository_files(tmp_path):
    files = {
        "source.py": "LegacyDirectory.lookup\n",
        "notes.txt": "Migration evidence\n",
    }
    for relative_path, content in files.items():
        (tmp_path / relative_path).write_text(content, encoding="utf-8")

    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    ImpactAnalyzer(tmp_path).analyze(_migration())

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_rejects_paths_outside_repository(tmp_path):
    migration = _migration(affected_files=("../outside.txt",))

    with pytest.raises(UnsafeRepositoryPathError):
        ImpactAnalyzer(tmp_path).analyze(migration)


def test_benchmark_metadata_integration():
    repository_root = Path(__file__).resolve().parents[1]
    metadata_path = repository_root / "demo" / "migration_benchmark" / "migration_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    migration = MigrationDescription.from_metadata(metadata)

    report = ImpactAnalyzer(repository_root).analyze(migration)

    impacted_paths = {impact.path for impact in report.impacted_files}
    assert {
        "demo/migration_benchmark/old/legacy_directory.py",
        "demo/migration_benchmark/old/profile_service.py",
        "demo/migration_benchmark/tests/test_old_baseline.py",
    } <= impacted_paths
    assert {
        "demo/migration_benchmark/target/profile_directory.py",
        "demo/migration_benchmark/target/profile_service.py",
        "demo/migration_benchmark/candidates/correct_migration/profile_service.py",
    } <= impacted_paths
    assert set(report.metadata_listed_files) == {
        "demo/migration_benchmark/old/legacy_directory.py",
        "demo/migration_benchmark/old/profile_service.py",
        "demo/migration_benchmark/tests/test_old_baseline.py",
    }
    assert any(
        reference.path == "demo/migration_benchmark/target/profile_service.py"
        and reference.symbol == "get_profile"
        for reference in report.target_references
    )
    old_service = next(
        impact
        for impact in report.impacted_files
        if impact.path == "demo/migration_benchmark/old/profile_service.py"
    )
    assert any("renamed to 'given_name'" in reason for reason in old_service.reasons)
    assert [impact.path for impact in report.impacted_files] == sorted(impacted_paths)
