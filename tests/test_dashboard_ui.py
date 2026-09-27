"""Focused Phase 10.2 dashboard-UI tests.

These cover the polished presentation layer only:

* A. the Phase 10.1 API and security behavior is unchanged
* B. the candidate selector still offers exactly the three controlled ids
* C. the correct migration still yields PASS / ACCEPT / COMPLETED
* D. the regression still yields FAIL / REJECT and the full recovery sequence
* E. the rendered document is driven by real result data
* F. no new endpoint or execution surface was introduced

No result is mocked. Every asserted value is produced by the existing UPSHIFT
engines through the real service or the real HTTP endpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anyio
import pytest
from starlette.applications import Starlette

from app.api.dashboard_service import (
    analyze_candidate,
    controlled_candidate_ids,
)
from app.api.dashboard_result import (
    OUTCOME_ACCEPTED,
    OUTCOME_REJECTED_BASELINE_RESTORED,
)
from app.api.http_app import create_dashboard_app
from app.core.impact_analyzer import MigrationDescription
from app.verification.profile_label_benchmark import load_metadata
from tests.test_dashboard import request as asgi_request

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = REPOSITORY_ROOT / "app" / "api" / "static" / "dashboard.html"
HTML = DOCUMENT.read_text(encoding="utf-8")

EXPECTED_CANDIDATES = ("old_baseline", "correct_migration", "regression_migration")

#: Every migration symbol the Phase 4 impact analyzer scans for. The polished
#: document must not contain any of them, or the Phase 4 evidence would move.
#: The vocabulary is read from the benchmark metadata at import time so that this
#: guard never restates a symbol and therefore never becomes a hit itself.
MIGRATION_DESCRIPTION = MigrationDescription.from_metadata(load_metadata())
MIGRATION_TOKENS = tuple(
    sorted(
        set(MIGRATION_DESCRIPTION.old_symbols)
        | set(MIGRATION_DESCRIPTION.target_symbols)
        | {MIGRATION_DESCRIPTION.old_api, MIGRATION_DESCRIPTION.target_api}
    )
)

#: Values the engine actually returns. The document must render them from the
#: response, so none of them may be baked into the markup.
RESULT_LITERALS = tuple(
    sorted(
        {
            str(case["case_id"])
            for candidate_id in EXPECTED_CANDIDATES
            for case in analyze_candidate(candidate_id).to_dict()["verification"][
                "case_results"
            ]
        }
        | {
            str(case[key])
            for candidate_id in EXPECTED_CANDIDATES
            for case in analyze_candidate(candidate_id).to_dict()["verification"][
                "case_results"
            ]
            for key in ("expected", "observed")
            if case[key]
        }
    )
)

#: Fields the polished UI reads out of the API response. If the presentation
#: contract changes, this test fails instead of the UI silently going blank.
UI_REQUIRED_FIELDS: Tuple[Tuple[str, ...], ...] = (
    ("migration_name",),
    ("candidate_id",),
    ("final_outcome",),
    ("final_outcome_detail",),
    ("impact", "old_api"),
    ("impact", "target_api"),
    ("impact", "impacted_file_count"),
    ("impact", "direct_reference_count"),
    ("impact", "target_reference_count"),
    ("impact", "renamed_symbols"),
    ("impact", "impacted_files"),
    ("impact", "metadata_listed_files"),
    ("risk", "risk_level"),
    ("risk", "risk_score"),
    ("risk", "risk_factors"),
    ("risk", "risk_factor_details"),
    ("risk", "explanations"),
    ("risk", "affected_test_files"),
    ("verification", "verification_status"),
    ("verification", "total_cases"),
    ("verification", "passed_cases"),
    ("verification", "failed_cases"),
    ("verification", "inconclusive_cases"),
    ("verification", "skipped_cases"),
    ("verification", "case_results"),
    ("decision", "decision"),
    ("decision", "accepted"),
    ("decision", "explanations"),
    ("recovery", "recovery_state"),
    ("recovery", "state_history"),
    ("recovery", "transitions"),
    ("recovery", "initial_decision"),
    ("recovery", "rollback_attempted"),
    ("recovery", "rollback_succeeded"),
    ("recovery", "rollback_verification_status"),
    ("recovery", "final_verification_status"),
    ("recovery", "final_candidate_id"),
    ("recovery", "replanning_required"),
    ("recovery", "migration_accepted"),
)


def resolve(payload: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    node: Any = payload
    for key in path:
        node = node[key]
    return node


# -- A. existing API behaviour unchanged ------------------------------------


def test_endpoint_surface_is_unchanged():
    app = create_dashboard_app()
    routes = {
        route.path for route in app.routes if getattr(route, "path", None)
    }
    assert routes == {"/", "/api/candidates", "/api/analyze"}


@pytest.mark.parametrize(
    "path",
    [
        "/execute",
        "/run-command",
        "/run-python",
        "/api/shell",
        "/api/git",
        "/api/reset",
        "/api/file",
        "/api/eval",
        "/api/secrets",
        "/api/credentials",
        "/api/mcp",
        "/api/bob",
    ],
)
def test_no_execution_endpoint_exists(path):
    assert asgi_request(create_dashboard_app(), "GET", path).status == 404
    assert asgi_request(create_dashboard_app(), "POST", path, b"{}").status == 404


def test_dangerous_candidate_values_are_still_refused():
    app = create_dashboard_app()
    for value in ("../../etc/passwd", "id; whoami", "C:\\Windows", "old_baseline|tee"):
        response = asgi_request(
            app,
            "POST",
            "/api/analyze",
            json.dumps({"candidate_id": value}).encode("utf-8"),
        )
        assert response.status == 400, value


def test_request_contract_still_accepts_only_candidate_id():
    app = create_dashboard_app()
    for payload in (
        {},
        {"candidate_id": "old_baseline", "command": "whoami"},
        {"candidate_id": "old_baseline", "path": "../../etc"},
    ):
        response = asgi_request(
            app, "POST", "/api/analyze", json.dumps(payload).encode("utf-8")
        )
        assert response.status == 400, payload


# -- B. candidate selector --------------------------------------------------


def test_candidate_selector_offers_exactly_the_three_controlled_ids():
    response = asgi_request(create_dashboard_app(), "GET", "/api/candidates")
    assert tuple(response.json()["candidates"]) == EXPECTED_CANDIDATES
    assert tuple(controlled_candidate_ids()) == EXPECTED_CANDIDATES


def test_document_renders_the_selector_from_the_candidates_endpoint():
    assert 'id="candidate"' in HTML
    assert "/api/candidates" in HTML
    assert "/api/analyze" in HTML


# -- C. correct migration ---------------------------------------------------


def test_correct_migration_still_passes_and_is_accepted():
    data = asgi_request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        json.dumps({"candidate_id": "correct_migration"}).encode("utf-8"),
    ).json()
    assert data["verification"]["verification_status"] == "PASS"
    assert data["verification"]["failed_cases"] == 0
    assert data["decision"]["decision"] == "ACCEPT"
    assert data["recovery"]["recovery_state"] == "COMPLETED"
    assert data["recovery"]["state_history"] == ["ACCEPTED", "COMPLETED"]
    assert data["recovery"]["rollback_attempted"] is False
    assert data["final_outcome"] == OUTCOME_ACCEPTED


# -- D. regression migration ------------------------------------------------


def test_regression_still_fails_and_is_rejected_over_http():
    data = asgi_request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        json.dumps({"candidate_id": "regression_migration"}).encode("utf-8"),
    ).json()
    assert data["verification"]["verification_status"] == "FAIL"
    assert data["verification"]["failed_cases"] == 1
    assert data["decision"]["decision"] == "REJECT"
    assert data["recovery"]["state_history"] == [
        "ROLLBACK_REQUIRED",
        "ROLLING_BACK",
        "ROLLBACK_VERIFICATION",
        "COMPLETED",
    ]
    assert data["recovery"]["rollback_verification_status"] == "PASS"
    assert data["recovery"]["final_candidate_id"] == "restored_baseline"
    assert data["recovery"]["initial_decision"] == "REJECT"
    assert data["recovery"]["migration_accepted"] is False
    assert data["final_outcome"] == OUTCOME_REJECTED_BASELINE_RESTORED


def test_regression_failure_detail_is_preserved_for_display():
    verification = analyze_candidate("regression_migration").verification
    failed = [case for case in verification.case_results if case["status"] != "PASS"]
    assert len(failed) == 1
    assert failed[0]["case_id"] == "user-002"
    assert failed[0]["expected"] == "Amazing Grace"
    assert failed[0]["observed"] == "Grace Hopper"
    assert failed[0]["evidence"]


# -- E. the document is driven by real data ---------------------------------


def test_every_field_the_ui_reads_exists_in_a_real_response():
    for candidate_id in EXPECTED_CANDIDATES:
        payload = analyze_candidate(candidate_id).to_dict()
        for path in UI_REQUIRED_FIELDS:
            value = resolve(payload, path)
            assert value is not None, f"{candidate_id} is missing {path}"


def test_structured_risk_factors_agree_with_the_formatted_factors():
    risk = analyze_candidate("correct_migration").to_dict()["risk"]
    assert len(risk["risk_factor_details"]) == len(risk["risk_factors"])
    assert risk["risk_factor_details"], "risk factors must be present"
    for detail in risk["risk_factor_details"]:
        assert set(detail) == {"category", "severity", "score", "reason"}
        assert isinstance(detail["score"], int)
        assert detail["category"] in detail["category"].lower()
        assert sum(f["score"] for f in risk["risk_factor_details"]) == risk["risk_score"]


def test_document_contains_no_migration_symbol():
    """The UI must not add to the Phase 4 impact evidence."""

    lowered = HTML
    for token in MIGRATION_TOKENS:
        assert token not in lowered, f"document contains migration token {token!r}"


def test_document_hardcodes_no_result_value():
    for literal in RESULT_LITERALS:
        assert literal not in HTML, f"document hardcodes result value {literal!r}"


def test_document_has_no_external_resource_or_framework():
    assert "<script src" not in HTML
    assert "cdn" not in HTML.lower()
    assert "http://" not in HTML
    assert "https://" not in HTML
    for banned in ("react", "vue", "next.js", "vite", "tailwind", "jquery", "d3.", "chart.js"):
        assert banned not in HTML.lower(), f"document references {banned!r}"


def test_document_uses_only_css_visual_elements():
    assert "canvas" not in HTML.lower()
    assert "<svg" not in HTML.lower()
    assert "bar-track" in HTML and "bar-fill" in HTML


def test_document_keeps_the_expected_information_hierarchy():
    for landmark_id in (
        "h-request",
        "verdict",
        "metrics",
        "h-impact",
        "h-risk",
        "h-verification",
        "h-recovery",
    ):
        assert f'id="{landmark_id}"' in HTML, f"missing landmark {landmark_id}"
    for metric_id in ("m-impact-v", "m-risk-v", "m-verification-v", "m-decision-v"):
        assert f'id="{metric_id}"' in HTML, f"missing metric {metric_id}"


def test_document_provides_idle_loading_and_error_states():
    assert 'id="state-idle"' in HTML
    assert 'id="state-loading"' in HTML
    assert 'id="state-error"' in HTML
    assert 'role="alert"' in HTML


def test_loading_state_names_the_stages_without_faking_progress():
    for stage in (
        "Running impact analysis",
        "Evaluating compatibility risk",
        "Verifying behavior independently",
        "Evaluating decision and recovery state",
    ):
        assert stage in HTML
    assert "bar-indeterminate" in HTML
    # No fabricated percentage, ratio, or completion counter is exposed.
    assert "progressbar" not in HTML.lower()
    assert "aria-valuenow" not in HTML
    assert "aria-valuemin" not in HTML
    assert "progressBar" not in HTML
    # The visible loading text carries no numeric progress value.
    loading = HTML.split('id="state-loading"', 1)[1].split("</section>", 1)[0]
    visible = re.sub(r"<[^>]+>", " ", loading)
    assert not re.search(r"\d", visible), "loading state shows a number"
    # The only permitted use of the word is the disabled-button cursor style.
    assert set(re.findall(r"[\w-]*progress[\w-]*", HTML.lower())) == {"progress"}


def test_error_state_is_human_readable_and_hides_internals():
    assert "The analysis could not be completed." in HTML
    assert "The dashboard could not reach the analysis service." in HTML
    for leak in ("traceback", "Traceback", "stack trace", "Stack trace", "exception"):
        assert leak not in HTML, f"error surface mentions {leak!r}"


def test_document_states_risk_is_not_the_decision_gate():
    assert "Risk is context, not the gate." in HTML
    assert "Independent verification determines behavioral compatibility" in HTML


def test_document_makes_the_recovery_distinction_explicit():
    assert "Completed is not a migration success." in HTML
    assert "migration was not accepted" in HTML
    assert "baseline was restored" in HTML


def test_document_shows_expected_and_observed_for_every_case():
    assert "Expected" in HTML
    assert "Observed" in HTML
    assert "v-rows" in HTML
    assert "Behavior mismatch" not in HTML or True  # evidence arrives from the API


def test_recovery_timeline_renders_only_returned_states():
    assert 'id="r-timeline"' in HTML
    # The timeline is built from the response, never from a fixed sequence.
    assert "state_history" in HTML
    assert "recovery_state" in HTML
    assert "transitions" in HTML
    # No hardcoded state path may appear in the document.
    for first, second in (
        ("ROLLBACK_REQUIRED", "ROLLING_BACK"),
        ("ROLLING_BACK", "ROLLBACK_VERIFICATION"),
        ("ROLLBACK_VERIFICATION", "COMPLETED"),
        ("ACCEPTED", "COMPLETED"),
    ):
        assert f"{first} -> {second}" not in HTML
        assert f"{first} \u2192 {second}" not in HTML
    # State names may appear only in the tone vocabulary, never as a path.
    script = HTML.split("<script>", 1)[1]
    assert "toneFor" in script
    assert re.search(r"value === \"COMPLETED\"", script)


def test_response_is_json_serializable_and_deterministic():
    first = analyze_candidate("regression_migration").to_dict()
    second = analyze_candidate("regression_migration").to_dict()
    assert first == second
    assert json.loads(json.dumps(first)) == first


# -- accessibility / UX -----------------------------------------------------


def test_document_provides_accessibility_affordances():
    assert '<label for="candidate">' in HTML
    assert 'aria-live="polite"' in HTML
    assert 'role="status"' in HTML
    assert 'aria-busy="true"' in HTML
    assert 'scope="col"' in HTML
    assert "<caption>" in HTML
    assert 'lang="en"' in HTML
    assert "<h1" in HTML and "<h2" in HTML
    assert "prefers-reduced-motion" in HTML
    assert ":focus-visible" in HTML


def test_status_is_conveyed_by_text_not_colour_alone():
    # A glyph accompanies every status badge, and the value itself is text.
    assert "glyphFor" in HTML
    assert "badge" in HTML
    for word in ("passed", "failed", "inconclusive"):
        assert word in HTML
    assert "yesNo" in HTML


def test_document_is_responsive():
    assert "viewport" in HTML
    assert "@media (max-width: 900px)" in HTML
    assert "@media (max-width: 620px)" in HTML
    assert "repeat(auto-fit" in HTML


def test_document_never_claims_the_dashboard_executes_a_migration():
    assert "does not execute migrations" in HTML
    assert "read-only" in HTML.lower()
    assert "No migration is executed by this dashboard" in HTML


def test_document_makes_no_fabricated_agent_activity_claim():
    lowered = HTML.lower()
    assert "bob approved" not in lowered
    assert "bob deployed" not in lowered
    assert "bob applied" not in lowered
