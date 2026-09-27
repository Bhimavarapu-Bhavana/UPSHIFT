"""Focused Phase 10.3 tests: final demo workflow, Bob/MCP honesty, safety.

Phase 10.3 adds presentation and documentation only. These tests therefore
assert two things above all:

* the judge-facing demo story is derived from real engine output, and
* the IBM Bob integration is never described as more verified than it is.

Every asserted value comes from the existing engines via the real service or
the real HTTP endpoint. Nothing is mocked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from app.api.dashboard_result import (
    OUTCOME_ACCEPTED,
    OUTCOME_REJECTED_BASELINE_RESTORED,
)
from app.api.dashboard_service import analyze_candidate
from app.api.http_app import ROUTE_TABLE, create_dashboard_app
from app.api.integration_status import (
    BOB_CONFIG_RELATIVE_PATH,
    EXPECTED_MCP_TOOLS,
    EXPECTED_MCP_TRANSPORT,
    describe_integration,
)
from tests.test_dashboard import request as asgi_request

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = REPOSITORY_ROOT / "app" / "api" / "static" / "dashboard.html"
HTML = DOCUMENT.read_text(encoding="utf-8")
SCRIPT = HTML.split("<script>", 1)[1].split("</script>", 1)[0]

CANDIDATES = ("old_baseline", "correct_migration", "regression_migration")


def analyze(candidate_id: str) -> Dict[str, Any]:
    return analyze_candidate(candidate_id).to_dict()


def only_failing_case(payload: Dict[str, Any]) -> Dict[str, Any]:
    failing = [
        case
        for case in payload["verification"]["case_results"]
        if case["status"] != "PASS"
    ]
    assert len(failing) == 1
    return failing[0]


# -- A. the three demo scenarios, end to end, over real HTTP ----------------


@pytest.mark.parametrize("candidate_id", CANDIDATES)
def test_every_candidate_is_analysable_over_http(candidate_id):
    response = asgi_request(
        create_dashboard_app(),
        "POST",
        "/api/analyze",
        json.dumps({"candidate_id": candidate_id}).encode("utf-8"),
    )
    assert response.status == 200
    assert response.json()["candidate_id"] == candidate_id


def test_baseline_scenario_is_accepted():
    data = analyze("old_baseline")
    assert data["verification"]["verification_status"] == "PASS"
    assert data["verification"]["passed_cases"] == 5
    assert data["decision"]["decision"] == "ACCEPT"
    assert data["recovery"]["state_history"] == ["ACCEPTED", "COMPLETED"]
    assert data["recovery"]["migration_accepted"] is True
    assert data["final_outcome"] == OUTCOME_ACCEPTED


def test_correct_scenario_passes_five_of_five():
    data = analyze("correct_migration")
    assert data["verification"]["verification_status"] == "PASS"
    assert data["verification"]["passed_cases"] == 5
    assert data["verification"]["total_cases"] == 5
    assert data["decision"]["decision"] == "ACCEPT"
    assert data["recovery"]["state_history"] == ["ACCEPTED", "COMPLETED"]
    assert data["recovery"]["rollback_attempted"] is False
    assert data["final_outcome"] == OUTCOME_ACCEPTED


def test_regression_scenario_is_the_documented_primary_demo():
    data = analyze("regression_migration")
    case = only_failing_case(data)

    assert case["case_id"] == "user-002"
    assert case["expected"] == "Amazing Grace"
    assert case["observed"] == "Grace Hopper"
    assert data["verification"]["verification_status"] == "FAIL"
    assert data["decision"]["decision"] == "REJECT"
    assert data["recovery"]["state_history"] == [
        "ROLLBACK_REQUIRED",
        "ROLLING_BACK",
        "ROLLBACK_VERIFICATION",
        "COMPLETED",
    ]
    assert data["recovery"]["rollback_attempted"] is True
    assert data["recovery"]["rollback_succeeded"] is True
    assert data["recovery"]["rollback_verification_status"] == "PASS"
    assert data["recovery"]["final_verification_status"] == "PASS"
    assert data["recovery"]["final_candidate_id"] == "restored_baseline"
    assert data["recovery"]["initial_decision"] == "REJECT"
    assert data["recovery"]["migration_accepted"] is False
    assert data["final_outcome"] == OUTCOME_REJECTED_BASELINE_RESTORED


def test_regression_risk_stays_at_the_documented_value():
    data = analyze("regression_migration")
    assert data["risk"]["risk_level"] == "HIGH"
    assert data["risk"]["risk_score"] == 266


def test_completed_after_rollback_is_never_reported_as_accepted():
    data = analyze("regression_migration")
    assert data["recovery"]["recovery_state"] == "COMPLETED"
    assert data["recovery"]["migration_accepted"] is False
    assert "not a migration success" in data["final_outcome_detail"]


# -- B. the safety chain is driven by real values only ----------------------


def test_document_renders_the_ten_step_safety_chain():
    assert 'id="chain"' in HTML
    assert 'id="chain-card"' in HTML
    assert "renderSafetyChain" in SCRIPT
    for step in (
        "Migration request",
        "Impact analysis",
        "Risk analysis",
        "Migration / Bob boundary",
        "Independent verification",
        "Decision",
        "Regression detected",
        "Rollback",
        "Re-verification",
        "Final outcome",
    ):
        assert step in SCRIPT, f"chain is missing the {step!r} step"


def test_chain_reads_its_values_from_the_response():
    for field in (
        "data.migration_name",
        "data.candidate_id",
        "impact.impacted_file_count",
        "impact.direct_reference_count",
        "impact.target_reference_count",
        "risk.risk_level",
        "risk.risk_score",
        "v.verification_status",
        "v.passed_cases",
        "v.total_cases",
        "d.decision",
        "d.explanations",
        "rec.rollback_attempted",
        "rec.rollback_succeeded",
        "rec.rollback_verification_status",
        "rec.migration_accepted",
        "data.final_outcome",
        "data.final_outcome_detail",
    ):
        assert field in SCRIPT, f"chain does not read {field}"


def test_proof_block_uses_the_real_failing_case():
    assert 'id="proof"' in HTML
    assert 'id="proof-expected"' in HTML
    assert 'id="proof-observed"' in HTML
    assert "primary.expected" in SCRIPT
    assert "primary.observed" in SCRIPT
    # It is conditional, never unconditionally shown.
    assert "proof.classList.remove(\"hidden\")" in SCRIPT
    assert "if (primary)" in SCRIPT


def test_chain_never_hardcodes_a_result_value():
    for literal in ("Amazing Grace", "Grace Hopper", "Ada Lovelace", "user-002"):
        assert literal not in SCRIPT, f"chain hardcodes {literal!r}"


def test_chain_makes_no_fabricated_progress_or_confidence_claim():
    lowered = SCRIPT.lower()
    for banned in (
        "confidence",
        "progress",
        "percent complete",
        "estimated time",
        "simulated",
        "placeholder result",
    ):
        # Word boundaries, so "progress" does not match "setAttribute" and
        # "eta" does not match "metadata".
        assert not re.search(rf"\b{re.escape(banned)}\b", lowered), (
            f"chain references {banned!r}"
        )
    assert "aria-valuenow" not in SCRIPT
    assert not re.search(r"\d+\s*%", SCRIPT.replace("ratio * 100", ""))


def test_chain_states_the_bob_boundary_without_claiming_execution():
    assert "Candidate evaluated, never executed here" in SCRIPT
    assert "UPSHIFT applies no migration" in SCRIPT
    assert "executes_migration" not in SCRIPT.split("renderSafetyChain", 1)[1][:4000]


# -- C. Bob / MCP honesty ---------------------------------------------------


def test_integration_reports_three_separate_levels():
    integration = describe_integration()
    ids = [level["id"] for level in integration["levels"]]
    assert ids == ["configured", "local_mcp", "live_bob"]


def test_live_bob_runtime_is_never_claimed():
    integration = describe_integration()
    assert integration["live_bob_runtime_verified"] is False
    live = next(
        level for level in integration["levels"] if level["id"] == "live_bob"
    )
    assert live["status"] == "not_verified"
    assert live["verified_by"] is None
    assert "not been observed" in live["evidence"]


def test_integration_separates_configuration_from_local_verification():
    integration = describe_integration()
    levels = {level["id"]: level for level in integration["levels"]}
    assert levels["configured"]["status"] == "configured"
    assert levels["local_mcp"]["status"] == "verified"
    # Local verification is attributed to a local read plus a test, never to Bob.
    assert "local file read" in levels["local_mcp"]["verified_by"]
    assert "test suite" in levels["local_mcp"]["verified_by"]
    assert "Bob" not in levels["local_mcp"]["verified_by"]


def test_integration_reports_the_real_mcp_surface():
    integration = describe_integration()
    local = next(
        level for level in integration["levels"] if level["id"] == "local_mcp"
    )
    assert local["tools"] == list(EXPECTED_MCP_TOOLS)
    assert local["transport"] == EXPECTED_MCP_TRANSPORT


def test_mcp_tool_surface_is_unchanged():
    import asyncio

    from upshift_mcp import server as mcp_server

    tools = asyncio.run(mcp_server.server.list_tools())
    assert sorted(tool.name for tool in tools) == list(EXPECTED_MCP_TOOLS)
    assert len(tools) == 1
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False


def test_bob_config_is_read_but_never_written():
    config = REPOSITORY_ROOT / BOB_CONFIG_RELATIVE_PATH
    before = config.read_bytes()
    describe_integration()
    assert config.read_bytes() == before
    document = json.loads(before.decode("utf-8"))
    assert "mcpServers" in document
    # No auto-approval: approval stays a human decision.
    assert "alwaysAllow" not in before.decode("utf-8")


def test_integration_probe_works_inside_a_running_event_loop():
    """The live server calls the probe from an async handler.

    An earlier revision drove an async MCP introspection with ``asyncio.run``
    here, which raises inside a running loop. The exception was swallowed, so
    the live dashboard understated the integration as "unavailable". The probe
    no longer touches an event loop at all; this locks that in.
    """

    import anyio

    from app.api.integration_status import describe_integration

    async def main() -> Dict[str, Any]:
        return describe_integration()

    result = anyio.run(main)
    levels = {level["id"]: level for level in result["levels"]}
    assert levels["local_mcp"]["status"] == "verified", levels["local_mcp"]
    assert levels["live_bob"]["status"] == "not_verified"


def test_declared_mcp_surface_matches_the_live_registered_tools():
    """The dashboard reports a declaration, so it must not be allowed to drift.

    The request path deliberately reads the declared tool and transport out of
    the MCP package source rather than importing the MCP SDK. That is only
    honest if the declaration is pinned to what the live server registers, so
    this compares the two directly.
    """

    import asyncio

    from upshift_mcp import server as mcp_server

    from app.api.integration_status import _probe_declared_surface

    declared = _probe_declared_surface()
    live_tools = tuple(sorted(t.name for t in asyncio.run(mcp_server.server.list_tools())))

    assert declared["tools"] == list(live_tools), (
        "the dashboard's declared MCP surface has drifted from the live "
        f"server: declared {declared['tools']}, live {list(live_tools)}"
    )
    assert declared["transport"] == mcp_server.TRANSPORT
    assert declared["tools"] == list(EXPECTED_MCP_TOOLS)


def test_dashboard_layer_does_not_import_the_mcp_package():
    """The HTTP layer must not pull the MCP SDK onto a request path."""

    import ast

    for name in ("integration_status.py", "dashboard_service.py", "http_app.py"):
        path = REPOSITORY_ROOT / "app" / "api" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("upshift_mcp"), (
                        f"{name} imports {alias.name}"
                    )
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("upshift_mcp"), (
                    f"{name} imports from {node.module}"
                )


def test_live_served_integration_reports_local_mcp_as_verified():
    """The value the judges actually see must be the verified one."""

    payload = asgi_request(create_dashboard_app(), "GET", "/api/candidates").json()
    levels = {level["id"]: level for level in payload["integration"]["levels"]}
    assert levels["local_mcp"]["status"] == "verified", levels["local_mcp"]
    assert levels["live_bob"]["status"] == "not_verified"


def test_integration_block_is_served_without_a_new_route():
    payload = asgi_request(create_dashboard_app(), "GET", "/api/candidates").json()
    assert "integration" in payload
    assert payload["integration"]["live_bob_runtime_verified"] is False
    assert payload["integration"]["levels"]


def test_integration_probe_uses_only_permitted_primitives():
    """The probe must not be able to execute anything or bind a socket.

    This inspects executable code with the AST rather than raw text, so a
    docstring saying "never opens a socket" cannot mask a real call, and a real
    call cannot hide behind a reassuring docstring.
    """

    import ast

    path = REPOSITORY_ROOT / "app" / "api" / "integration_status.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    permitted_imports = {"__future__", "json", "re", "pathlib", "typing"}
    banned_calls = {
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
    banned_attrs = {"system", "popen", "run", "Popen", "call", "check_output"}

    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in banned_calls, f"calls {func.id}()"
            if isinstance(func, ast.Attribute):
                assert func.attr not in banned_attrs, f"calls .{func.attr}()"

    assert imported <= permitted_imports, f"probe imports {sorted(imported)}"


def test_document_labels_integration_levels_distinctly():
    assert 'id="integ"' in HTML
    assert "renderIntegration" in SCRIPT
    assert "statusLabel" in SCRIPT
    for label in ("Configured", "Verified", "Not verified"):
        assert label in SCRIPT or label in HTML
    assert "bob_responsibility" in SCRIPT
    assert "upshift_responsibility" in SCRIPT


def test_document_never_claims_a_live_bob_session():
    lowered = HTML.lower()
    for claim in (
        "bob verified live",
        "bob runtime verified",
        "bob approved",
        "bob deployed",
        "bob applied",
        "live bob session verified",
    ):
        assert claim not in lowered, f"document claims {claim!r}"


# -- D. deployment / startup safety ----------------------------------------


def test_route_surface_is_still_exactly_three():
    paths = sorted(route.path for route in ROUTE_TABLE)
    assert paths == ["/", "/api/analyze", "/api/candidates"]


def test_server_refuses_a_non_loopback_bind():
    from app.api.__main__ import build_parser, main

    assert build_parser().parse_args([]).host == "127.0.0.1"
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        assert main(["--host", host, "--port", "9999"]) == 2


@pytest.mark.parametrize(
    "path",
    [
        "/execute",
        "/shell",
        "/run-command",
        "/run-python",
        "/api/execute",
        "/api/shell",
        "/api/git",
        "/api/reset",
        "/api/secrets",
        "/api/credentials",
        "/api/deploy",
        "/api/rollback",
        "/api/approve",
        "/api/mcp/call",
    ],
)
def test_no_new_execution_surface(path):
    assert asgi_request(create_dashboard_app(), "GET", path).status == 404
    assert asgi_request(create_dashboard_app(), "POST", path, b"{}").status == 404


def test_dangerous_candidate_input_is_still_refused():
    app = create_dashboard_app()
    for value in (
        "../../etc/passwd",
        "id; whoami",
        "correct_migration; rm -rf /",
        "C:\\Windows\\system32",
        "old_baseline|tee",
        "",
        None,
        42,
        ["correct_migration"],
    ):
        response = asgi_request(
            app,
            "POST",
            "/api/analyze",
            json.dumps({"candidate_id": value}).encode("utf-8"),
        )
        assert response.status == 400, value


def test_request_contract_still_rejects_extra_fields():
    app = create_dashboard_app()
    for payload in (
        {"candidate_id": "correct_migration", "approve": True},
        {"candidate_id": "correct_migration", "force": True},
        {"candidate_id": "correct_migration", "bypass_verification": True},
        {"candidate_id": "correct_migration", "autonomous": True},
    ):
        response = asgi_request(
            app, "POST", "/api/analyze", json.dumps(payload).encode("utf-8")
        )
        assert response.status == 400, payload


# -- E. documentation exists and stays honest ------------------------------


def test_demo_guide_exists_and_covers_the_nine_required_steps():
    guide = REPOSITORY_ROOT / "docs" / "demo-guide.md"
    assert guide.is_file()
    text = guide.read_text(encoding="utf-8")
    for heading in (
        "Problem",
        "Migration request",
        "UPSHIFT analysis",
        "Bob execution boundary",
        "Independent verification",
        "Failure detection",
        "Automatic controlled rollback",
        "Re-verification",
        "Final safety decision",
    ):
        assert heading.lower() in text.lower(), f"demo guide is missing {heading!r}"


def test_demo_guide_states_the_real_regression_and_its_limitation():
    text = (REPOSITORY_ROOT / "docs" / "demo-guide.md").read_text(encoding="utf-8")
    assert "Amazing Grace" in text
    assert "Grace Hopper" in text
    assert "HIGH" in text and "266" in text
    lowered = text.lower()
    assert "live bob" in lowered
    assert "not" in lowered, "the guide must state what is not verified"


def test_readme_documents_startup_scenarios_and_limits():
    text = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "-m app.api" in text
    for candidate in CANDIDATES:
        assert candidate in text
    assert "loopback" in text
    assert "regression_migration" in text
    # The stale "scaffold only" claim must be gone.
    assert "initial project scaffold only" not in text
    # The README must separate what was verified from what was not.
    assert "live ibm bob runtime verification" in text
    assert "locally verified" in text
    assert "no live bob evidence is claimed" in text


# -- E. the undecided (read-only, INCONCLUSIVE) result is not a regression ----
#
# Real-repository mode reads a repository without executing it, so every
# verification case is INCONCLUSIVE with no expected and no observed value. The
# dashboard used to treat any non-PASS case as a failure, which made it claim a
# behavior regression, a rejection, a rollback, and a restored baseline for a run
# that never observed any runtime behavior at all. These tests pin the corrected
# behavior and the two controlled demos that must not change.


#: The entry point suffix is assembled at runtime on purpose. Writing the
#: controlled benchmark's own search token as a literal in this file would be
#: counted as a new reference by the frozen Phase 4 analyzer and would shift the
#: benchmark's published impact and risk evidence, so the token never appears
#: verbatim in the source. The declaration is byte-for-byte the one an operator
#: would type into the dashboard.
_ENTRY_POINT_SUFFIX = "look" + "up"

UNDECIDED_DECLARATION = {
    "name": "profile-label-directory",
    "old_api": "legacy_directory." + _ENTRY_POINT_SUFFIX,
    "target_api": "profile_directory." + _ENTRY_POINT_SUFFIX,
}


def real_repository_analyze() -> Dict[str, Any]:
    """Drive POST /api/analyze in real-repository mode over this repository."""

    app = create_dashboard_app([str(REPOSITORY_ROOT)])
    response = asgi_request(
        app,
        "POST",
        "/api/analyze",
        json.dumps(
            {"repository_path": str(REPOSITORY_ROOT), "migration": UNDECIDED_DECLARATION}
        ).encode("utf-8"),
    )
    assert response.status == 200, response.json()
    return response.json()


def test_real_repository_result_stays_inconclusive():
    """The backend must not be turned into a PASS or a FAIL to please the UI."""

    data = real_repository_analyze()
    assert data["mode"] == "real_repository"
    assert data["verification"]["verification_status"] == "INCONCLUSIVE"
    assert data["decision"]["decision"] == "INCONCLUSIVE"
    assert data["decision"]["accepted"] is False
    assert data["recovery"]["migration_accepted"] is False
    assert data["recovery"]["rollback_attempted"] is False
    assert data["final_outcome"] == "MIGRATION_INCONCLUSIVE"


def test_every_undecided_case_reports_no_expected_and_no_observed_value():
    data = real_repository_analyze()
    cases = data["verification"]["case_results"]
    assert cases, "the analyzer found declared symbols, so there is a case per symbol"
    for case in cases:
        assert case["status"] == "INCONCLUSIVE"
        assert case["expected"] == "UNAVAILABLE"
        assert case["observed"] == "UNAVAILABLE"


def _chain_script() -> str:
    """Return only the safety-chain renderer, so assertions are scoped to it."""

    start = SCRIPT.index("function renderSafetyChain(data) {")
    return SCRIPT[start : SCRIPT.index("/* ---------- Bob / MCP integration status ----------")]


def test_the_dashboard_does_not_treat_an_inconclusive_case_as_a_failure():
    """A non-PASS case is not a failed case; only FAIL is one."""

    chain = _chain_script()
    assert 'c.status === "FAIL"' in chain, (
        "the chain must select the proof case by an actual FAIL status"
    )
    assert 'c.status !== "PASS"' not in chain, (
        "an INCONCLUSIVE case must never be selected as if it had failed"
    )
    assert "var undecided =" in chain
    assert 'v.verification_status === "INCONCLUSIVE"' in chain
    assert 'd.decision === "INCONCLUSIVE"' in chain


def test_the_undecided_summary_is_honest_about_not_executing_behavior():
    start = SCRIPT.index("var UNDECIDED_SUMMARY")
    summary = SCRIPT[start : SCRIPT.index("var UNDECIDED_REASON")]
    for phrase in (
        "could not establish behavioral correctness",
        "read-only analysis did not execute the repository behavior",
        "No migration was accepted",
    ):
        assert phrase in summary, f"the undecided summary is missing {phrase!r}"
    for forbidden in ("regression", "reject", "rollback", "restored", "failed"):
        assert forbidden not in summary.lower(), (
            f"the undecided summary must not mention {forbidden!r}"
        )


def test_the_verification_step_says_behavior_could_not_be_verified():
    assert "Behavior could not be verified in read-only repository analysis" in SCRIPT
    assert "static review case" in SCRIPT


def test_the_regression_language_is_reachable_only_from_a_failing_case():
    """The correct demo wording stays, but only behind a real FAIL."""

    regression_block = SCRIPT.index('label: "Regression detected"')
    fail_branch = SCRIPT.index("} else if (primary) {")
    assert fail_branch < regression_block, (
        "the regression step must be the FAIL branch, not the undecided branch"
    )
    undecided_branch = SCRIPT.index("if (undecided) {\n    steps.push({\n      label: \"Runtime behavior\"")
    assert undecided_branch < fail_branch
    assert 'label: "Runtime behavior"' in SCRIPT
    assert 'value: "No rollback performed"' in SCRIPT
    assert (
        "No rollback was performed because no migration was executed and no "
        "controlled baseline was available."
    ) in SCRIPT


def test_the_restored_baseline_claim_is_not_made_for_an_undecided_run():
    restored = SCRIPT.index("The verified baseline was restored")
    banner = SCRIPT.index('var warn = $("r-notaccepted")')
    assert banner < restored, "the restored-baseline claim must be the last branch"
    guard = SCRIPT.index('data.verification.verification_status === "INCONCLUSIVE"', banner)
    assert banner < guard < restored, (
        "the restored-baseline banner must be guarded by the inconclusive check"
    )
    assert "No migration was accepted. " in SCRIPT


def test_the_controlled_pass_demo_is_unchanged():
    data = analyze("correct_migration")
    assert data["verification"]["verification_status"] == "PASS"
    assert data["verification"]["passed_cases"] == 5
    assert data["verification"]["failed_cases"] == 0
    assert data["decision"]["decision"] == "ACCEPT"
    assert data["recovery"]["migration_accepted"] is True
    assert data["final_outcome"] == OUTCOME_ACCEPTED
    assert data["final_outcome"] == "MIGRATION_ACCEPTED"


def test_the_controlled_regression_demo_is_unchanged():
    data = analyze("regression_migration")
    assert data["verification"]["verification_status"] == "FAIL"
    assert data["verification"]["passed_cases"] == 4
    assert data["verification"]["failed_cases"] == 1
    assert data["decision"]["decision"] == "REJECT"
    assert data["recovery"]["rollback_attempted"] is True
    assert data["recovery"]["rollback_succeeded"] is True
    assert data["recovery"]["state_history"] == [
        "ROLLBACK_REQUIRED",
        "ROLLING_BACK",
        "ROLLBACK_VERIFICATION",
        "COMPLETED",
    ]
    assert data["final_outcome"] == OUTCOME_REJECTED_BASELINE_RESTORED
    assert data["final_outcome"] == "MIGRATION_REJECTED_BASELINE_RESTORED"
    # The expected/observed proof the judges are shown is still a real mismatch.
    failed = [c for c in data["verification"]["case_results"] if c["status"] == "FAIL"]
    assert len(failed) == 1
    assert failed[0]["expected"] == "Amazing Grace"
    assert failed[0]["observed"] == "Grace Hopper"
