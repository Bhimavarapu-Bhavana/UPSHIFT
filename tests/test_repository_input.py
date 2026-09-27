"""Focused tests for UPSHIFT real-repository mode.

What these tests hold in place:

* the boundary is closed by default and cannot be widened by a request;
* a refused path or declaration is never echoed back;
* the reader never executes, follows a link, writes, or reads a credential;
* the reader is deterministic and stays inside its budgets;
* real-repository mode reports honest INCONCLUSIVE evidence and never accepts;
* the controlled benchmark is untouched by any of it, and the frozen Phase 4
  engine is still byte-for-byte the file the project locked.

Repository fixtures are built in ``tmp_path`` only. No test reads, writes, or
depends on a real user's source tree.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from app.api.dashboard_result import MODE_DEMO, MODE_REAL_REPOSITORY
from app.api.dashboard_service import analyze_candidate
from app.api.http_app import ANALYZE_REQUEST_SHAPES, ROUTE_TABLE, create_dashboard_app
from app.api.repository_service import (
    REAL_REPOSITORY_BOUNDARY,
    analyze_repository,
    describe_analysis_modes,
    real_repository_cases,
)
from app.repository.evidence import (
    DEFAULT_LIMITS,
    SKIP_BINARY,
    SKIP_FILE_BUDGET,
    SKIP_OVERSIZED,
    SKIP_SENSITIVE,
    SKIP_UNSUPPORTED_TYPE,
    RepositoryLimits,
)
from app.repository.real_input import (
    NoControlledRollbackProvider,
    RealRepositoryInput,
    RepositoryInput,
    RepositoryReadOnlyError,
)
from app.repository.snapshot import SNAPSHOT_PREFIX, bounded_snapshot
from app.security.repository_input import (
    RepositoryBoundaryError,
    RepositoryDeclarationError,
    build_repository_boundary,
    require_migration_declaration,
)
from app.verification.verifier import UNAVAILABLE, CandidateVerifier, VerificationCandidate

from tests.test_dashboard import post_json, request

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: A neutral, throwaway migration used by the repository fixtures. The names are
#: deliberately unrelated to the controlled benchmark so adding this file cannot
#: move the benchmark's own impact evidence.
OLD_API = "legacy_pkg.old_call"
TARGET_API = "modern_pkg.new_call"
MIGRATION: Dict[str, Any] = {
    "name": "fixture migration",
    "old_api": OLD_API,
    "target_api": TARGET_API,
    "renamed_symbols": [["old_call", "new_call"]],
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small, ordinary source tree to read."""

    root = tmp_path / "billing_service"
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "api.py").write_text(
        "def call(order):\n    return legacy_pkg.old_call(order.id)\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("Calls legacy_pkg.old_call today.\n", encoding="utf-8")
    return root


def digest_tree(root: Path) -> Dict[str, str]:
    """Content digest of every file in a tree, for read-only assertions."""

    out: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


# ---------------------------------------------------------------------------
# A. The boundary is closed by default
# ---------------------------------------------------------------------------


def test_no_boundary_is_configured_by_default():
    assert build_repository_boundary().is_configured is False
    assert build_repository_boundary([]).is_configured is False
    assert build_repository_boundary(None).is_configured is False


def test_closed_boundary_refuses_every_path(repo: Path):
    with pytest.raises(RepositoryBoundaryError):
        build_repository_boundary().resolve(str(repo))


def test_allowlist_does_not_grant_the_whole_filesystem(repo: Path, tmp_path: Path):
    boundary = build_repository_boundary([str(repo)])
    assert boundary.resolve(str(repo)) == repo.resolve()
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve(str(tmp_path))
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve(str(repo.parent))


def test_traversal_out_of_an_allowed_root_is_refused(repo: Path):
    boundary = build_repository_boundary([str(repo)])
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve(str(repo / ".." / ".." / "etc"))


def test_relative_path_is_refused(repo: Path):
    boundary = build_repository_boundary([str(repo)])
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve("billing_service")


def test_url_is_not_a_local_path(repo: Path):
    boundary = build_repository_boundary([str(repo)])
    for value in ("https://example.invalid/repo", "file:///etc", "ssh://host/repo"):
        with pytest.raises(RepositoryBoundaryError):
            boundary.resolve(value)


def test_missing_or_non_directory_target_is_refused(tmp_path: Path):
    boundary = build_repository_boundary([str(tmp_path)])
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve(str(tmp_path / "absent"))
    target = tmp_path / "a_file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve(str(target))


def test_control_characters_in_a_path_are_refused(repo: Path):
    boundary = build_repository_boundary([str(repo)])
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve(str(repo) + "\n")


def test_a_refusal_never_echoes_the_value(repo: Path, tmp_path: Path):
    secret = tmp_path / "private_customer_data"
    secret.mkdir()
    boundary = build_repository_boundary([str(repo)])
    with pytest.raises(RepositoryBoundaryError) as caught:
        boundary.resolve(str(secret))
    assert str(secret) not in str(caught.value)
    assert str(secret) not in " ".join(boundary.describe())


def test_a_symlink_out_of_the_boundary_is_refused(repo: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = repo / "escape"
    try:
        os.symlink(outside, escape, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
        pytest.skip("this platform does not permit creating directory symlinks")
    with pytest.raises(RepositoryBoundaryError):
        build_repository_boundary([str(repo)]).resolve(str(escape))


def test_operator_roots_are_resolved_deduplicated_and_sorted(repo: Path, tmp_path: Path):
    other = tmp_path / "aaa_other"
    other.mkdir()
    boundary = build_repository_boundary(
        [str(repo), str(other), str(repo), str(repo / "svc")]
    )
    assert boundary.roots == tuple(sorted({repo.resolve(), other.resolve(), (repo / "svc").resolve()}))


def test_a_bad_operator_root_fails_closed_at_construction(tmp_path: Path):
    with pytest.raises(RepositoryBoundaryError):
        build_repository_boundary([str(tmp_path / "does_not_exist")])
    with pytest.raises(RepositoryBoundaryError):
        build_repository_boundary(["relative/path"])


def test_boundary_description_never_names_a_path(repo: Path, tmp_path: Path):
    described = " ".join(build_repository_boundary([str(repo)]).describe())
    assert str(repo) not in described
    assert str(tmp_path) not in described


# ---------------------------------------------------------------------------
# B. The declaration is data, never a capability
# ---------------------------------------------------------------------------


def test_declaration_requires_the_three_named_fields():
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({"old_api": OLD_API, "target_api": TARGET_API})
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({"name": "n"})
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration("not an object")


def test_declaration_rejects_unexpected_fields():
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "command": "whoami"})
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "root": "/etc"})


@pytest.mark.parametrize(
    "symbol",
    [
        "../../etc/passwd",
        "pkg; rm -rf /",
        "pkg.func(",
        "pkg func",
        "pkg\nfunc",
        "pkg.func\n",
        "$(whoami)",
        "`id`",
        "",
        "   ",
    ],
)
def test_symbols_must_be_dotted_identifiers(symbol: str):
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "old_symbols": [symbol]})


def test_symbols_must_be_a_list_not_a_single_string():
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "old_symbols": "pkg.func"})


def test_renamed_symbols_must_be_identifier_pairs():
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "renamed_symbols": ["a", "b"]})
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "renamed_symbols": [["a"]]})
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "renamed_symbols": [["a", "b", "c"]]})


def test_omitted_symbols_are_left_to_the_frozen_phase_four_engine():
    """The declaration must not own a second vocabulary rule."""

    declaration = require_migration_declaration(
        {"name": "n", "old_api": OLD_API, "target_api": TARGET_API}
    )
    assert declaration.old_symbols == ()
    assert declaration.target_symbols == ()

    # The frozen engine is what fills them in, for both analysis modes.
    from app.api.dashboard_service import analyze_candidate as demo

    assert demo("correct_migration").impact.old_api


def test_a_declaration_refusal_never_echoes_the_value():
    with pytest.raises(RepositoryDeclarationError) as caught:
        require_migration_declaration({**MIGRATION, "command": "rm -rf /"})
    assert "rm -rf" not in str(caught.value)


def test_an_oversized_declaration_field_is_refused():
    with pytest.raises(RepositoryDeclarationError):
        require_migration_declaration({**MIGRATION, "name": "n" * 5000})


# ---------------------------------------------------------------------------
# C. The reader is read-only, bounded, and deterministic
# ---------------------------------------------------------------------------


def test_reader_satisfies_the_one_operation_protocol(repo: Path):
    assert isinstance(RealRepositoryInput(repo.resolve()), RepositoryInput)


def test_reader_returns_sorted_relative_text(repo: Path):
    load = RealRepositoryInput(repo.resolve()).load()
    assert load.files == tuple(sorted(load.files))
    assert [path for path, _ in load.files] == ["README.md", "svc/api.py"]
    assert all(not path.startswith(("/", "\\")) for path, _ in load.files)
    assert "C:" not in "".join(path for path, _ in load.files)


def test_two_reads_of_an_unchanged_repository_are_identical(repo: Path):
    first = RealRepositoryInput(repo.resolve()).load()
    second = RealRepositoryInput(repo.resolve()).load()
    assert first.files == second.files
    assert first.evidence == second.evidence


def test_reading_does_not_modify_the_repository(repo: Path):
    before = digest_tree(repo)
    RealRepositoryInput(repo.resolve()).load()
    assert digest_tree(repo) == before


def test_analysis_does_not_modify_the_repository(repo: Path):
    before = digest_tree(repo)
    analyze_repository(str(repo), MIGRATION, [str(repo)])
    assert digest_tree(repo) == before


def test_credential_and_key_files_are_never_read(repo: Path):
    (repo / ".env").write_text("TOKEN=shhh", encoding="utf-8")
    (repo / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    (repo / "id_ed25519.pub").write_text("ssh-ed25519", encoding="utf-8")
    (repo / "server.pem").write_text("-----BEGIN", encoding="utf-8")
    (repo / "credentials.json").write_text("{}", encoding="utf-8")
    (repo / "app_passwords.py").write_text("old_call", encoding="utf-8")

    load = RealRepositoryInput(repo.resolve()).load()
    read = {path for path, _ in load.files}
    assert "TOKEN=shhh" not in "".join(text for _, text in load.files)
    assert not any(Path(path).name.startswith((".env", "id_")) for path in read)
    assert "server.pem" not in read
    assert "credentials.json" not in read
    assert "app_passwords.py" not in read

    reasons = {summary.reason: summary for summary in load.evidence.skipped}
    assert reasons[SKIP_SENSITIVE].count == 6
    assert ".env" in reasons[SKIP_SENSITIVE].recorded_paths


def test_binary_content_is_detected_rather_than_guessed(repo: Path):
    (repo / "blob.py").write_bytes(b"\x00\x01legacy_pkg.old_call")
    load = RealRepositoryInput(repo.resolve()).load()
    assert "blob.py" not in {path for path, _ in load.files}
    reasons = {summary.reason for summary in load.evidence.skipped}
    assert SKIP_BINARY in reasons


def test_an_oversized_file_is_skipped(repo: Path):
    (repo / "huge.py").write_text("old_call " * 60_000, encoding="utf-8")
    load = RealRepositoryInput(repo.resolve()).load()
    assert "huge.py" not in {path for path, _ in load.files}
    reasons = {summary.reason: summary for summary in load.evidence.skipped}
    assert reasons[SKIP_OVERSIZED].count == 1
    assert load.evidence.total_bytes_read < DEFAULT_LIMITS.max_file_bytes


def test_generated_and_cached_directories_are_not_walked(repo: Path):
    for name in ("node_modules", ".git", "__pycache__", "dist", ".venv"):
        skipped = repo / name
        skipped.mkdir()
        (skipped / "module.py").write_text("old_call", encoding="utf-8")
    load = RealRepositoryInput(repo.resolve()).load()
    read = {path for path, _ in load.files}
    assert read == {"README.md", "svc/api.py"}
    assert set(load.evidence.ignored_directories) >= {
        ("node_modules", 1),
        (".git", 1),
        ("__pycache__", 1),
    }


def test_an_unsupported_file_type_is_skipped(repo: Path):
    (repo / "logo.png").write_bytes(b"\x89PNG")
    load = RealRepositoryInput(repo.resolve()).load()
    reasons = {summary.reason: summary for summary in load.evidence.skipped}
    assert reasons[SKIP_UNSUPPORTED_TYPE].count == 1
    assert "logo.png" in reasons[SKIP_UNSUPPORTED_TYPE].recorded_paths


def test_a_link_inside_the_repository_is_not_followed(repo: Path, tmp_path: Path):
    target = tmp_path / "outside_secrets.py"
    target.write_text("old_call from outside", encoding="utf-8")
    try:
        os.symlink(target, repo / "link.py")
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
        pytest.skip("this platform does not permit creating file symlinks")
    load = RealRepositoryInput(repo.resolve()).load()
    assert "link.py" not in {path for path, _ in load.files}
    assert "outside" not in "".join(text for _, text in load.files)


def test_the_file_budget_is_enforced_and_reported(repo: Path):
    for index in range(6):
        (repo / f"mod_{index}.py").write_text("old_call", encoding="utf-8")
    load = RealRepositoryInput(repo.resolve(), limits=RepositoryLimits(max_files=3)).load()
    assert len(load.files) == 3
    reasons = {summary.reason: summary for summary in load.evidence.skipped}
    assert SKIP_FILE_BUDGET in reasons
    assert load.evidence.limits.max_files == 3


def test_the_reader_refuses_a_root_that_is_not_a_directory(repo: Path):
    with pytest.raises(RepositoryReadOnlyError):
        RealRepositoryInput(repo / "svc" / "api.py")


def test_evidence_reports_the_restrictions_it_actually_applied(repo: Path):
    restrictions = " ".join(RealRepositoryInput(repo.resolve()).load().evidence.safety_restrictions)
    assert "read-only" in restrictions
    assert "never" in restrictions or "no repository file is executed" in restrictions


# ---------------------------------------------------------------------------
# D. Real-repository mode is honest about what it cannot know
# ---------------------------------------------------------------------------


def test_real_mode_is_closed_without_an_operator_root(repo: Path):
    with pytest.raises(RepositoryBoundaryError):
        analyze_repository(str(repo), MIGRATION, None)
    with pytest.raises(RepositoryBoundaryError):
        analyze_repository(str(repo), MIGRATION, [])


def test_real_mode_reports_inconclusive_and_never_accepts(repo: Path):
    result = analyze_repository(str(repo), MIGRATION, [str(repo)])
    data = result.to_dict()

    assert data["mode"] == MODE_REAL_REPOSITORY
    assert result.is_real_repository is True
    assert data["final_outcome"] == "MIGRATION_INCONCLUSIVE"
    assert data["decision"]["accepted"] is False
    assert data["verification"]["verification_status"] == "INCONCLUSIVE"
    assert data["verification"]["passed_cases"] == 0
    assert data["verification"]["failed_cases"] == 0
    assert data["verification"]["inconclusive_cases"] == data["verification"]["total_cases"]


def _impact_report_with(symbols: Tuple[str, ...]) -> Any:
    """A real Phase 4 report, built with the frozen public dataclasses."""

    from app.core.impact_analyzer import ImpactReport, SymbolReference

    return ImpactReport(
        migration_name="fixture migration",
        old_api=OLD_API,
        target_api=TARGET_API,
        old_symbols=symbols,
        target_symbols=(),
        renamed_symbols=(),
        impacted_files=(),
        direct_references=tuple(
            SymbolReference(path="svc/api.py", symbol=symbol, line=1, column=1)
            for symbol in symbols
        ),
        target_references=(),
        metadata_listed_files=(),
    )


def test_real_mode_cases_carry_no_expected_behavior():
    report = _impact_report_with(("old_call", "legacy_pkg"))
    cases = real_repository_cases(report)
    assert [case.case_id for case in cases] == [
        "static_review:legacy_pkg",
        "static_review:old_call",
    ]
    assert all(case.expected is UNAVAILABLE for case in cases)


def test_a_repository_with_no_reference_still_produces_one_honest_case():
    cases = real_repository_cases(_impact_report_with(()))
    assert len(cases) == 1
    assert cases[0].expected is UNAVAILABLE
    assert "No reference" in cases[0].description


def test_no_case_invokes_a_resolver():
    """The engine must not reach a resolver when nothing can be observed."""

    calls: List[str] = []

    def resolver(*args: Any, **kwargs: Any) -> Any:
        calls.append("called")
        raise AssertionError("a real repository must never be observed")

    report = CandidateVerifier().verify(
        VerificationCandidate(candidate_id="probe", path="fixture", resolve=resolver),
        real_repository_cases(_impact_report_with(("old_call",))),
        migration_name="fixture migration",
    )
    assert calls == []
    assert report.status == "INCONCLUSIVE"
    assert report.passed_cases == 0
    assert report.failed_cases == 0
    assert report.inconclusive_cases == 1


def test_real_mode_has_no_controlled_baseline(repo: Path):
    provider = NoControlledRollbackProvider()
    assert provider.rollback_available("real_repository") is False
    result = provider.restore_baseline("real_repository")
    assert result.available is False
    with pytest.raises(RepositoryReadOnlyError):
        provider.load_restored_baseline()

    recovery = analyze_repository(str(repo), MIGRATION, [str(repo)]).to_dict()["recovery"]
    assert recovery["rollback_attempted"] is False
    assert recovery["rollback_succeeded"] is False
    assert recovery["migration_accepted"] is False
    # Verification is INCONCLUSIVE, so the existing engine justifies neither
    # acceptance nor a rollback, and it says so.
    assert recovery["recovery_state"] == "INCONCLUSIVE"
    assert recovery["replanning_required"] is False
    explanations = " ".join(recovery["explanations"])
    assert "no acceptance is claimed" in explanations
    assert "No rollback was attempted" in explanations


def test_the_reported_restoration_mechanism_is_the_absent_one(repo: Path):
    data = analyze_repository(str(repo), MIGRATION, [str(repo)]).to_dict()
    assert data["repository"]["rollback_mechanism"].startswith("no_controlled_baseline")
    assert analyze_candidate("regression_migration").to_dict()["mode"] == MODE_DEMO


def test_real_mode_result_never_carries_a_filesystem_path(repo: Path):
    data = analyze_repository(str(repo), MIGRATION, [str(repo)]).to_dict()
    body = json.dumps(data, sort_keys=True)
    assert str(repo) not in body
    assert str(repo.parent) not in body
    assert ":\\" not in body
    assert data["repository"]["repository_name"] == repo.name
    assert "repository_path" not in json.dumps(data["repository"])


def test_real_mode_evidence_names_what_was_and_was_not_read(repo: Path):
    (repo / ".env").write_text("TOKEN=shhh", encoding="utf-8")
    data = analyze_repository(str(repo), MIGRATION, [str(repo)]).to_dict()
    repository = data["repository"]
    assert repository["inspected_paths"] == ["README.md", "svc/api.py"]
    assert repository["skipped_file_count"] == 1
    assert repository["skipped"][0]["reason"] == SKIP_SENSITIVE
    assert repository["total_bytes_read"] > 0
    assert repository["safety_restrictions"]


def test_two_real_analyses_of_an_unchanged_repository_are_identical(repo: Path):
    first = analyze_repository(str(repo), MIGRATION, [str(repo)]).to_dict()
    second = analyze_repository(str(repo), MIGRATION, [str(repo)]).to_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_a_repository_with_no_declared_symbol_still_reports_honestly(repo: Path):
    result = analyze_repository(
        str(repo),
        {
            "name": "unrelated migration",
            "old_api": "absent_pkg.absent_call",
            "target_api": "other_pkg.other_call",
        },
        [str(repo)],
    )
    data = result.to_dict()
    assert data["impact"]["impacted_file_count"] == 0
    assert data["verification"]["total_cases"] == 1
    assert data["verification"]["inconclusive_cases"] == 1
    assert data["final_outcome"] == "MIGRATION_INCONCLUSIVE"


def test_the_scratch_snapshot_is_removed_after_analysis(repo: Path):
    """The frozen engine is given a bounded copy, and the copy does not survive."""

    import app.api.repository_service as service

    seen: List[Path] = []
    real_snapshot = service.bounded_snapshot

    class _Recording:
        def __init__(self, manager: Any) -> None:
            self._manager = manager

        def __enter__(self) -> Path:
            scratch = self._manager.__enter__()
            seen.append(scratch)
            return scratch

        def __exit__(self, *exc: object) -> Any:
            return self._manager.__exit__(*exc)

    service.bounded_snapshot = lambda files, **kwargs: _Recording(
        real_snapshot(files, **kwargs)
    )
    try:
        analyze_repository(str(repo), MIGRATION, [str(repo)])
    finally:
        service.bounded_snapshot = real_snapshot

    assert seen, "the frozen engine must be given a bounded copy to read"
    assert all(SNAPSHOT_PREFIX in scratch.name for scratch in seen)
    assert not any(scratch.exists() for scratch in seen)


def test_a_snapshot_label_cannot_escape_the_scratch_root(tmp_path: Path):
    with pytest.raises(ValueError):
        with bounded_snapshot([("../escape.py", "x")]):
            pass
    with pytest.raises(ValueError):
        with bounded_snapshot([("/absolute.py", "x")]):
            pass


def test_a_snapshot_is_removed_even_when_analysis_fails():
    created: List[Path] = []
    with pytest.raises(RuntimeError):
        with bounded_snapshot([("a.py", "x")]) as scratch:
            created.append(scratch)
            raise RuntimeError("boom")
    assert created and not created[0].exists()


# ---------------------------------------------------------------------------
# E. The controlled benchmark is untouched
# ---------------------------------------------------------------------------


def test_the_controlled_benchmark_result_is_unchanged():
    data = analyze_candidate("correct_migration").to_dict()
    assert data["mode"] == MODE_DEMO
    assert data["repository"] is None
    assert data["candidate_id"] == "correct_migration"
    assert data["final_outcome"] == "MIGRATION_ACCEPTED"


def test_real_mode_adds_no_impact_evidence_to_the_benchmark():
    benchmark = analyze_candidate("correct_migration").to_dict()["impact"]
    assert benchmark["impacted_file_count"] > 0
    assert not any(name.startswith("app/repository") for name in benchmark["impacted_files"])
    assert not any(name.startswith("tests/test_repository_input") for name in benchmark["impacted_files"])


def test_the_frozen_impact_analyzer_is_byte_for_byte_the_locked_file():
    """Real-repository mode must reuse Phase 4, not rewrite it."""

    from tests.test_recovery_engine import PROTECTED_SHA256

    locked = PROTECTED_SHA256["app/core/impact_analyzer.py"]
    digest = hashlib.sha256(
        (REPOSITORY_ROOT / "app" / "core" / "impact_analyzer.py").read_bytes()
    ).hexdigest()
    assert digest == locked, "real-repository mode must not modify the frozen engine"


def test_the_frozen_engine_receives_only_the_bounded_snapshot(repo: Path):
    """What Phase 4 sees is the read, not the repository on disk."""

    seen: List[Tuple[str, ...]] = []
    import app.api.repository_service as service

    real_snapshot = service.bounded_snapshot

    def recording(files: Any, **kwargs: Any) -> Any:
        seen.append(tuple(path for path, _ in files))
        return real_snapshot(files, **kwargs)

    service.bounded_snapshot = recording
    try:
        analyze_repository(str(repo), MIGRATION, [str(repo)])
    finally:
        service.bounded_snapshot = real_snapshot

    assert seen == [("README.md", "svc/api.py")]


def test_the_boundary_policy_is_stated_for_the_reader():
    modes = describe_analysis_modes(None)
    assert modes["real_repository"]["available"] is False
    assert modes["real_repository"]["reads_repository_files"] is True
    assert modes["real_repository"]["executes_repository_code"] is False
    assert modes["real_repository"]["migration_declaration_required"] is True
    assert REAL_REPOSITORY_BOUNDARY in (
        modes["real_repository"]["summary"],
        modes["real_repository"]["reason"],
    )


# ---------------------------------------------------------------------------
# F. The HTTP surface stays three routes and two exact shapes
# ---------------------------------------------------------------------------


def test_the_route_surface_is_unchanged():
    assert {route.path for route in ROUTE_TABLE} == {
        "/",
        "/api/candidates",
        "/api/analyze",
    }


def test_exactly_two_analyze_shapes_are_accepted():
    assert ANALYZE_REQUEST_SHAPES == (
        frozenset({"candidate_id"}),
        frozenset({"repository_path", "migration"}),
    )


def test_the_controlled_benchmark_still_answers(repo: Path):
    app = create_dashboard_app([str(repo)])
    body = request(
        app, "POST", "/api/analyze", post_json("/api/analyze", {"candidate_id": "old_baseline"})
    ).json()
    assert body["mode"] == MODE_DEMO
    assert body["repository"] is None


def test_real_repository_mode_is_refused_when_no_root_was_allowed(repo: Path):
    app = create_dashboard_app()
    payload = {"repository_path": str(repo), "migration": MIGRATION}
    response = request(app, "POST", "/api/analyze", post_json("/api/analyze", payload))
    assert response.status == 400
    assert str(repo) not in response.text


def test_real_repository_mode_answers_when_a_root_was_allowed(repo: Path):
    app = create_dashboard_app([str(repo)])
    payload = {"repository_path": str(repo), "migration": MIGRATION}
    response = request(app, "POST", "/api/analyze", post_json("/api/analyze", payload))
    assert response.status == 200
    assert response.json()["mode"] == MODE_REAL_REPOSITORY


def test_a_request_can_never_widen_the_boundary(repo: Path, tmp_path: Path):
    app = create_dashboard_app([str(repo)])
    other = tmp_path / "other_repo"
    other.mkdir()
    payload = {"repository_path": str(other), "migration": MIGRATION}
    response = request(app, "POST", "/api/analyze", post_json("/api/analyze", payload))
    assert response.status == 400
    assert app.state.allowed_repository_roots == (str(repo.resolve()),)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"repository_path": "x"},
        {"migration": {}},
        {"candidate_id": "old_baseline", "repository_path": "/"},
        {"candidate_id": "old_baseline", "command": "whoami"},
    ],
)
def test_a_mixed_or_partial_shape_is_refused(payload: Dict[str, Any]):
    app = create_dashboard_app()
    response = request(app, "POST", "/api/analyze", post_json("/api/analyze", payload))
    assert response.status == 400


def test_the_modes_block_reports_the_boundary_without_a_path(repo: Path):
    closed = request(create_dashboard_app(), "GET", "/api/candidates").json()["modes"]
    assert closed["demo"]["available"] is True
    assert closed["real_repository"]["available"] is False
    assert closed["real_repository"]["executes_repository_code"] is False
    assert "boundary" in closed["real_repository"]["reason"]

    opened = request(
        create_dashboard_app([str(repo)]), "GET", "/api/candidates"
    ).json()["modes"]
    assert opened["real_repository"]["available"] is True
    assert str(repo) not in json.dumps(opened)


def test_a_long_repository_path_is_refused_by_the_size_cap(repo: Path):
    app = create_dashboard_app([str(repo)])
    payload = {
        "repository_path": str(repo) + ("\\deep" * 1200),
        "migration": MIGRATION,
    }
    assert request(app, "POST", "/api/analyze", post_json("/api/analyze", payload)).status == 413


# ---------------------------------------------------------------------------
# G. The entry point only widens the boundary when an operator says so
# ---------------------------------------------------------------------------


def test_the_cli_exposes_a_repeatable_operator_flag() -> None:
    from app.api.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["--allow-repository-root", "/a", "--allow-repository-root", "/b"]
    )
    assert args.allow_repository_root == ["/a", "/b"]
    assert build_parser().parse_args([]).allow_repository_root == []


def test_the_cli_refuses_to_start_on_a_bad_operator_root(capsys: Any) -> None:
    from app.api.__main__ import main

    assert main(["--allow-repository-root", "/definitely/not/here"]) == 2
    assert "refusing to start" in capsys.readouterr().out


def test_the_cli_still_refuses_a_non_loopback_bind() -> None:
    from app.api.__main__ import main

    assert main(["--host", "0.0.0.0"]) == 2


# ---------------------------------------------------------------------------
# H. No module in this path gained an execution capability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "app/repository/__init__.py",
        "app/repository/evidence.py",
        "app/repository/real_input.py",
        "app/repository/snapshot.py",
        "app/api/repository_service.py",
    ],
)
def test_the_read_path_imports_nothing_that_executes(relative: str) -> None:
    """No process, network, or dynamic-loading capability on the read path.

    ``tempfile`` and ``shutil`` are permitted only in the snapshot bridge, where
    they create and delete UPSHIFT's own scratch copy of an already-bounded read.
    They cannot reach a user's repository, and the deletion is what keeps the
    read from leaving anything behind.
    """

    import ast

    forbidden = {
        "subprocess",
        "socket",
        "ctypes",
        "multiprocessing",
        "urllib",
        "http",
        "requests",
        "httpx",
        "ftplib",
        "smtplib",
        "webbrowser",
        "importlib",
        "runpy",
        "pickle",
        "marshal",
        "shutil",
    }
    allowed_here = {"tempfile", "shutil"} if relative.endswith("snapshot.py") else set()
    tree = ast.parse((REPOSITORY_ROOT / relative).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    offending = (roots & forbidden) - allowed_here
    assert not offending, f"{relative} imports {sorted(offending)}"


def test_the_read_path_never_uses_eval_or_exec() -> None:
    import ast

    for relative in (
        "app/repository/real_input.py",
        "app/repository/snapshot.py",
        "app/api/repository_service.py",
    ):
        tree = ast.parse((REPOSITORY_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}
            if isinstance(node, ast.Attribute):
                assert not node.attr.startswith("system")
