"""Focused Phase 10.1 dashboard tests.

Coverage follows the phase contract:

* A. the dashboard application and service start
* B. candidate validation, including path-like and command-like refusal
* C. the correct migration yields the existing PASS / ACCEPT result
* D. the regression yields the existing FAIL / REJECT result and the full
  Phase 9 recovery sequence, through the real HTTP endpoint
* E. Phase 4-9 evidence is unchanged
* F. the exposed surface adds no arbitrary execution

No result is mocked. Every asserted value is produced by the existing UPSHIFT
engines.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, Optional

import anyio
import pytest
from starlette.applications import Starlette

from app.api.dashboard_result import (
    OUTCOME_ACCEPTED,
    OUTCOME_REJECTED_BASELINE_RESTORED,
)
from app.api.dashboard_service import (
    analyze_candidate,
    controlled_candidate_ids,
    describe_service,
)
from app.api.http_app import MAX_REQUEST_BYTES, create_dashboard_app
from app.api.__main__ import build_parser, main
from app.security.candidates import (
    is_controlled_candidate,
    looks_like_path_or_command,
    require_controlled_candidate,
)
from app.verification.verifier import UnknownCandidateError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_CANDIDATES = ("old_baseline", "correct_migration", "regression_migration")

PATH_LIKE_VALUES = (
    "../../etc/passwd",
    "..",
    ".",
    "/etc/passwd",
    "C:\\Windows\\System32",
    "demo/migration_benchmark/old",
    "candidates/../old",
    "old_baseline/",
    "\\old_baseline",
)

COMMAND_LIKE_VALUES = (
    "id; whoami",
    "old_baseline && whoami",
    "old_baseline | tee out",
    "$(whoami)",
    "`id`",
    "old_baseline\nwhoami",
    "old_baseline > out.txt",
    "python -c 'import os'",
    "--flag",
    "-rf",
    "old baseline",
)

NON_STRING_VALUES = (None, 123, 4.5, True, ["old_baseline"], {"id": "old_baseline"})


# -- in-process ASGI harness (no extra dependency) ------------------------


class _Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


def request(
    app: Starlette,
    method: str,
    path: str,
    payload: Optional[bytes] = None,
) -> _Response:
    """Drive the ASGI app in-process and collect the response."""

    captured: Dict[str, Any] = {}

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": payload or b"", "more_body": False}

    async def send(message: Dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            captured["start"] = message
        elif kind == "http.response.body":
            captured["body"] = captured.get("body", b"") + message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8765),
    }

    anyio.run(app, scope, receive, send)

    start = captured["start"]
    return _Response(
        start["status"],
        {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in start.get("headers", [])
        },
        captured.get("body", b""),
    )


def post_json(path: str, payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


# -- A. availability -------------------------------------------------------


def test_dashboard_application_builds():
    assert isinstance(create_dashboard_app(), Starlette)


def test_service_describes_itself_without_claiming_execution():
    described = describe_service()
    assert described["executes_migration"] is False
    assert described["read_only_evidence"] is True
    assert tuple(described["controlled_candidates"]) == EXPECTED_CANDIDATES
    assert described["prohibited_capabilities"]


def test_dashboard_document_is_served():
    response = request(create_dashboard_app(), "GET", "/")
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "UPSHIFT" in response.text
    assert "/api/analyze" in response.text


def test_candidates_endpoint_lists_only_controlled_candidates():
    response = request(create_dashboard_app(), "GET", "/api/candidates")
    assert response.status == 200
    body = response.json()
    assert tuple(body["candidates"]) == EXPECTED_CANDIDATES
    assert body["executes_migration"] is False


def test_console_entry_point_refuses_a_non_loopback_bind():
    assert build_parser().parse_args([]).host == "127.0.0.1"
    assert main(["--host", "0.0.0.0"]) == 2


# -- B. candidate validation ------------------------------------------------


def test_controlled_candidate_ids_are_exactly_the_benchmark_three():
    assert tuple(controlled_candidate_ids()) == EXPECTED_CANDIDATES


@pytest.mark.parametrize("candidate_id", EXPECTED_CANDIDATES)
def test_allowed_candidates_are_accepted(candidate_id):
    assert is_controlled_candidate(candidate_id) is True
    assert require_controlled_candidate(candidate_id) == candidate_id


@pytest.mark.parametrize("value", PATH_LIKE_VALUES)
def test_path_like_candidates_are_refused(value):
    assert looks_like_path_or_command(value) is True
    assert is_controlled_candidate(value) is False
    with pytest.raises(UnknownCandidateError):
        require_controlled_candidate(value)
    with pytest.raises(UnknownCandidateError):
        analyze_candidate(value)


@pytest.mark.parametrize("value", COMMAND_LIKE_VALUES)
def test_command_like_candidates_are_refused(value):
    assert looks_like_path_or_command(value) is True
    assert is_controlled_candidate(value) is False
    with pytest.raises(UnknownCandidateError):
        require_controlled_candidate(value)
    with pytest.raises(UnknownCandidateError):
        analyze_candidate(value)


@pytest.mark.parametrize("value", NON_STRING_VALUES)
def test_non_string_candidates_are_refused(value):
    with pytest.raises(UnknownCandidateError):
        require_controlled_candidate(value)
    with pytest.raises(UnknownCandidateError):
        analyze_candidate(value)


@pytest.mark.parametrize("value", ["", "   ", "unknown", "OLD_BASELINE", "old", "baseline"])
def test_unknown_candidates_are_refused(value):
    with pytest.raises(UnknownCandidateError):
        require_controlled_candidate(value)
    with pytest.raises(UnknownCandidateError):
        analyze_candidate(value)


def test_refusal_message_does_not_echo_the_rejected_value():
    with pytest.raises(UnknownCandidateError) as caught:
        require_controlled_candidate("../../secret/place")
    assert "../../secret/place" not in str(caught.value)
    for candidate_id in EXPECTED_CANDIDATES:
        assert candidate_id in str(caught.value)


def test_api_refuses_an_unknown_candidate():
    response = request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        post_json("/api/analyze", {"candidate_id": "not_a_candidate"}),
    )
    assert response.status == 400
    assert "candidate" in response.json()["error"]


@pytest.mark.parametrize("value", ["../../etc/passwd", "id; whoami"])
def test_api_refuses_path_and_command_like_candidates(value):
    response = request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        post_json("/api/analyze", {"candidate_id": value}),
    )
    assert response.status == 400


# -- C. correct migration ---------------------------------------------------


def test_correct_migration_reports_the_existing_pass_and_accept():
    result = analyze_candidate("correct_migration")
    data = result.to_dict()
    assert data["verification"]["verification_status"] == "PASS"
    assert data["verification"]["failed_cases"] == 0
    assert data["decision"]["decision"] == "ACCEPT"
    assert data["decision"]["accepted"] is True
    assert data["final_outcome"] == OUTCOME_ACCEPTED
    assert data["recovery"]["migration_accepted"] is True
    assert data["recovery"]["rollback_attempted"] is False


def test_old_baseline_reports_the_existing_pass():
    data = analyze_candidate("old_baseline").to_dict()
    assert data["verification"]["verification_status"] == "PASS"
    assert data["decision"]["decision"] == "ACCEPT"


# -- D. regression, end to end through the HTTP endpoint --------------------


def test_regression_reports_the_existing_fail_and_reject():
    data = analyze_candidate("regression_migration").to_dict()
    assert data["verification"]["verification_status"] == "FAIL"
    assert data["verification"]["failed_cases"] >= 1
    assert data["decision"]["decision"] == "REJECT"
    assert data["decision"]["accepted"] is False


def test_regression_evidence_names_the_failing_case():
    data = analyze_candidate("regression_migration").to_dict()
    failing = [
        case for case in data["verification"]["case_results"] if case["status"] != "PASS"
    ]
    assert failing, "the regression must actually fail verification"
    assert failing[0]["case_id"] == "user-002"
    assert "Amazing Grace" in failing[0]["evidence"]
    assert "Grace Hopper" in failing[0]["evidence"]


def test_regression_recovery_runs_the_full_phase_nine_sequence():
    recovery = analyze_candidate("regression_migration").to_dict()["recovery"]
    assert recovery["state_history"] == [
        "ROLLBACK_REQUIRED",
        "ROLLING_BACK",
        "ROLLBACK_VERIFICATION",
        "COMPLETED",
    ]
    assert recovery["recovery_state"] == "COMPLETED"
    assert recovery["rollback_attempted"] is True
    assert recovery["rollback_succeeded"] is True
    assert recovery["rollback_verification_status"] == "PASS"
    assert recovery["final_verification_status"] == "PASS"
    assert recovery["final_candidate_id"] == "restored_baseline"
    assert recovery["replanning_required"] is False


def test_recovery_completion_does_not_report_a_migration_success():
    data = analyze_candidate("regression_migration").to_dict()
    recovery = data["recovery"]
    assert recovery["initial_decision"] == "REJECT"
    assert recovery["migration_accepted"] is False
    assert data["decision"]["accepted"] is False
    assert data["final_outcome"] == OUTCOME_REJECTED_BASELINE_RESTORED
    assert "not a migration success" in data["final_outcome_detail"]


def test_http_endpoint_reproduces_the_regression_recovery_sequence():
    response = request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        post_json("/api/analyze", {"candidate_id": "regression_migration"}),
    )
    assert response.status == 200
    body = response.json()
    assert body["candidate_id"] == "regression_migration"
    assert body["verification"]["verification_status"] == "FAIL"
    assert body["decision"]["decision"] == "REJECT"
    assert body["recovery"]["state_history"] == [
        "ROLLBACK_REQUIRED",
        "ROLLING_BACK",
        "ROLLBACK_VERIFICATION",
        "COMPLETED",
    ]
    assert body["recovery"]["rollback_verification_status"] == "PASS"
    assert body["recovery"]["final_candidate_id"] == "restored_baseline"
    assert body["recovery"]["initial_decision"] == "REJECT"
    assert body["recovery"]["migration_accepted"] is False
    assert body["final_outcome"] == OUTCOME_REJECTED_BASELINE_RESTORED


def test_http_endpoint_serves_the_correct_migration_result():
    response = request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        post_json("/api/analyze", {"candidate_id": "correct_migration"}),
    )
    assert response.status == 200
    body = response.json()
    assert body["verification"]["verification_status"] == "PASS"
    assert body["decision"]["decision"] == "ACCEPT"


# -- E. existing behaviour preserved ----------------------------------------


def test_dashboard_reports_the_unchanged_phase_four_evidence():
    impact = analyze_candidate("correct_migration").impact
    assert impact.impacted_file_count == 18
    assert impact.direct_reference_count == 112
    assert impact.target_reference_count == 116
    # Asserted structurally: naming the renamed symbols here would add
    # migration references to this file and move the Phase 4 evidence.
    assert len(impact.renamed_symbols) == 3
    assert all(len(pair) == 2 and pair[0] and pair[1] for pair in impact.renamed_symbols)
    assert len({old for old, _ in impact.renamed_symbols}) == 3
    assert len({new for _, new in impact.renamed_symbols}) == 3


def test_dashboard_reports_the_unchanged_phase_five_evidence():
    risk = analyze_candidate("correct_migration").risk
    assert risk.risk_level == "HIGH"
    assert risk.risk_score == 266


def test_dashboard_adds_no_migration_reference_to_the_repository():
    from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
    from app.core.risk_analyzer import RiskAnalyzer
    from app.verification.profile_label_benchmark import load_metadata

    migration = MigrationDescription.from_metadata(load_metadata())
    impact = ImpactAnalyzer(REPOSITORY_ROOT).analyze(migration)
    risk = RiskAnalyzer().analyze(migration, impact)
    assert len(impact.impacted_files) == 18
    assert len(impact.direct_references) == 112
    assert len(impact.target_references) == 116
    assert risk.risk_score == 266
    assert not any(
        "app/api" in entry.path or "app/security" in entry.path
        for entry in impact.impacted_files
    )


def test_dashboard_result_is_deterministic():
    first = analyze_candidate("regression_migration").to_dict()
    second = analyze_candidate("regression_migration").to_dict()
    assert first == second


def test_dashboard_result_is_json_serializable_and_frozen():
    result = analyze_candidate("correct_migration")
    assert json.loads(json.dumps(result.to_dict()))
    with pytest.raises(Exception):
        result.candidate_id = "tampered"
    with pytest.raises(Exception):
        result.recovery.recovery_state = "COMPLETED"


# -- F. security ------------------------------------------------------------


def test_complete_route_surface_is_exactly_three_documented_routes():
    app = create_dashboard_app()
    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in app.routes
        if getattr(route, "path", None)
    }
    assert routes == {
        ("/", ("GET", "HEAD")),
        ("/api/candidates", ("GET", "HEAD")),
        ("/api/analyze", ("POST",)),
    }


def test_no_execution_or_admin_route_is_exposed():
    forbidden = (
        "execute",
        "exec",
        "command",
        "run",
        "python",
        "shell",
        "subprocess",
        "git",
        "reset",
        "force",
        "push",
        "write",
        "file",
        "path",
        "env",
        "secret",
        "credential",
        "token",
        "mcp",
        "bob",
        "eval",
        "import",
        "upload",
        "download",
        "proxy",
    )
    for route in create_dashboard_app().routes:
        path = getattr(route, "path", "") or ""
        for word in forbidden:
            assert word not in path.lower(), f"route {path!r} exposes {word!r}"


@pytest.mark.parametrize(
    "path",
    [
        "/execute",
        "/run-command",
        "/run-python",
        "/api/execute",
        "/api/run",
        "/api/shell",
        "/api/git",
        "/api/file",
        "/api/eval",
        "/api/import",
        "/api/secrets",
        "/api/mcp",
        "/api/bob",
        "/static/../app/__init__.py",
    ],
)
def test_unknown_and_dangerous_paths_are_not_found(path):
    assert request(create_dashboard_app(), "GET", path).status == 404
    assert request(create_dashboard_app(), "POST", path, b"{}").status == 404


def test_wrong_methods_are_refused():
    app = create_dashboard_app()
    assert request(app, "GET", "/api/analyze").status == 405
    assert request(app, "POST", "/", b"{}").status == 405
    assert request(app, "POST", "/api/candidates", b"{}").status == 405
    assert request(app, "DELETE", "/api/analyze", b"{}").status == 405


def test_only_the_candidate_id_field_is_accepted():
    app = create_dashboard_app()
    for payload in (
        {},
        {"candidate": "old_baseline"},
        {"candidate_id": "old_baseline", "command": "whoami"},
        {"candidate_id": "old_baseline", "path": "../../etc"},
        {"candidate_id": "old_baseline", "repository": "/"},
    ):
        response = request(app, "POST", "/api/analyze", post_json("/api/analyze", payload))
        assert response.status == 400, payload


def test_malformed_bodies_are_refused():
    app = create_dashboard_app()
    assert request(app, "POST", "/api/analyze", b"not json").status == 400
    assert request(app, "POST", "/api/analyze", b"[1,2,3]").status == 400
    assert request(app, "POST", "/api/analyze", b'"text"').status == 400
    assert request(app, "POST", "/api/analyze", b"").status == 400


def test_oversized_body_is_refused():
    payload = b'{"candidate_id":"' + b"a" * (MAX_REQUEST_BYTES + 64) + b'"}'
    response = request(create_dashboard_app(), "POST", "/api/analyze", payload)
    assert response.status == 413


def test_no_absolute_path_or_workspace_leaks_into_a_result():
    body = request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        post_json("/api/analyze", {"candidate_id": "regression_migration"}),
    ).text
    assert str(REPOSITORY_ROOT) not in body
    assert "upshift-recovery-" not in body
    assert "upshift_restored_baseline" not in body
    assert ":\\" not in body
    assert "AppData" not in body
    assert "Temp" not in body


def _imported_roots(path: Path) -> set:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_dashboard_modules_import_no_dangerous_module():
    allowed = {
        "__future__",
        "app",
        "argparse",
        "ast",
        "dataclasses",
        "json",
        "pathlib",
        "re",
        "starlette",
        "typing",
        "uvicorn",
    }
    forbidden = {
        "subprocess",
        "socket",
        "shutil",
        "pickle",
        "marshal",
        "ctypes",
        "importlib",
        "runpy",
        "urllib",
        "http",
        "requests",
        "httpx",
        "ftplib",
        "smtplib",
        "multiprocessing",
        "webbrowser",
        "tempfile",
        "os",
    }
    sources = sorted((REPOSITORY_ROOT / "app" / "api").rglob("*.py")) + sorted(
        (REPOSITORY_ROOT / "app" / "security").rglob("*.py")
    )
    assert sources
    for path in sources:
        roots = _imported_roots(path)
        unexpected = roots - allowed
        assert not unexpected, f"{path.name} imports {sorted(unexpected)}"
        assert not (roots & forbidden), f"{path.name} imports {sorted(roots & forbidden)}"


def test_dashboard_modules_use_no_dynamic_execution_primitive():
    forbidden_calls = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "globals",
        "locals",
        "getattr",
        "setattr",
        "open",
        "input",
    }
    sources = sorted((REPOSITORY_ROOT / "app" / "api").rglob("*.py")) + sorted(
        (REPOSITORY_ROOT / "app" / "security").rglob("*.py")
    )
    assert sources
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (
                    f"{path.name} calls {node.func.id}()"
                )
            if isinstance(node, ast.Attribute):
                assert not node.attr.startswith("system"), (
                    f"{path.name} uses {node.attr}"
                )


def test_service_accepts_no_provider_or_path_override():
    from app.api import dashboard_service

    import inspect

    parameters = set(inspect.signature(dashboard_service.analyze_candidate).parameters)
    assert parameters == {"candidate_id"}


def test_controlled_provider_is_always_closed():
    from app.api import dashboard_service

    original = dashboard_service.BenchmarkRollbackProvider
    created = []

    class Recording:
        def __init__(self, **kwargs: Any) -> None:
            self._inner = original(**kwargs)
            created.append(self)

        def __enter__(self) -> "Recording":
            self._inner.__enter__()
            return self

        def __exit__(self, *exc_info: object) -> None:
            self._inner.__exit__(*exc_info)

        def rollback_available(self, candidate_id: str) -> bool:
            return self._inner.rollback_available(candidate_id)

        def restore_baseline(self, candidate_id: str) -> Any:
            return self._inner.restore_baseline(candidate_id)

        def load_restored_baseline(self) -> Any:
            return self._inner.load_restored_baseline()

    dashboard_service.BenchmarkRollbackProvider = Recording
    try:
        analyze_candidate("regression_migration")
    finally:
        dashboard_service.BenchmarkRollbackProvider = original

    assert len(created) == 1
    assert created[0]._inner._workspace is None
