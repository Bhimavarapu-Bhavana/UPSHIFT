"""Focused tests for the Phase 9 recovery layer.

These tests cover the recovery state machine, the controlled rollback
abstraction, the immutable recovery report, determinism, and the safety
boundaries that keep a failed migration from being reported as successful.

The suite deliberately drives the real Phase 4-7 components and the real
controlled rollback provider, so an integration regression cannot hide behind a
mock.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import tokenize
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any, Optional, Tuple

import pytest

from app.core.decision_engine import (
    ACCEPT as DECISION_ACCEPT,
    INCONCLUSIVE as DECISION_INCONCLUSIVE,
    REJECT as DECISION_REJECT,
    DecisionEvidence,
    DecisionReport,
    MigrationDecisionEngine,
)
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.recovery_engine import (
    ACCEPTED,
    COMPLETED,
    INCONCLUSIVE,
    NOT_RUN,
    RECOVERED_BASELINE_NOTE,
    RECOVERY_FAILED,
    RECOVERY_STATES,
    REPLANNING,
    ROLLBACK_REQUIRED,
    ROLLBACK_VERIFICATION,
    ROLLING_BACK,
    MigrationRecoveryEngine,
    RecoveryEvidence,
    RecoveryInputError,
    RecoveryReport,
    ReplanRequest,
    RollbackProvider,
    RollbackResult,
    StateTransition,
)
from app.core.risk_analyzer import LOW, RiskReport, RiskAnalyzer
from app.verification.benchmark_rollback import (
    BASELINE_CANDIDATE_ID,
    RESTORED_CANDIDATE_ID,
    WORKSPACE_KIND,
    BenchmarkRollbackProvider,
)
from app.verification.profile_label_benchmark import (
    build_cases,
    load_candidate,
    load_metadata,
    verify_candidate,
)
from app.verification.verifier import (
    FAIL as VERIFICATION_FAIL,
    INCONCLUSIVE as VERIFICATION_INCONCLUSIVE,
    PASS as VERIFICATION_PASS,
    UnknownCandidateError,
    VerificationCandidate,
    VerificationCase,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = REPOSITORY_ROOT / "app" / "core" / "recovery_engine.py"
PROVIDER_PATH = REPOSITORY_ROOT / "app" / "verification" / "benchmark_rollback.py"

MIGRATION_NAME = "profile-label-directory"
REGRESSION_CANDIDATE = "regression_migration"
CORRECT_CANDIDATE = "correct_migration"

# Recorded Phase 4-7 and Phase 8.1/8.2 SHA-256 digests. Phase 9 must not change
# any of them; test 18 asserts this.
PROTECTED_SHA256 = {
    "app/core/impact_analyzer.py": "55ff7386e176ee32aa42946865d7a2b4f702be2219509ec5079e3ffd40c11764",
    "tests/test_impact_analyzer.py": "51e19c57b387ac5f0f696243e4f111df39ffe43fe50cf302d9da02cb9b3d751d",
    "app/core/risk_analyzer.py": "d329b801dbcad163e914ed7c7fb7bd93d47a957b0b57ee76b7db37d17d25df67",
    "tests/test_risk_analyzer.py": "df5b21e61a99b22c781bce10f9c53a0d906ab0e29550c39aa58d3ffdcfa0dff5",
    "app/verification/verifier.py": "4766ce548123d788fc1891ce8e19fbecb29286e859e891a4e75bdaab0a6e6f54",
    "tests/test_verifier.py": "21a604fbb85bae3cdb7e2039cf271691329e17979859a034a2e2f248c6a941b6",
    "app/core/decision_engine.py": "f9b9a82a0071b13d4dcbf9284192c99db9ad180a079f26f60181bafd8e7dc92d",
    "tests/test_decision_engine.py": "bfc72dcf439e81446b2b720b7b042de072c782c473b4fd3ba12969fde6ed8ae7",
}

PHASE_EIGHT_SHA256 = {
    "upshift_mcp/__init__.py": "792b23e7fb9513cb276e244c1e6addd2d0e47d27021238b6922e7443f1397f37",
    "upshift_mcp/__main__.py": "7e011c2dbefa89bb5727a7983aae2306a14f8399f69d91e8bc51afb1adf9d9d6",
}

# Phase 11.1 added the operator-approved real-repository bridge, so the MCP
# surface legitimately changed: two more inputs on the same single tool, and one
# new module holding the operator approval. These digests replace the Phase 8
# ones for exactly those files and keep the tripwire armed from here on. The
# Phase 4-7 engines in PROTECTED_SHA256 are untouched by that phase.
PHASE_ELEVEN_ONE_SHA256 = {
    "upshift_mcp/context.py": "7a0c9784b2530dee2d5f4e3004d10338795002dd6bf5387923f6820f292711e6",
    "upshift_mcp/server.py": "4fb6c0e29bcd14b9ac6d89c3423de2f89cacfcffaf6e2f0959c7a0882405ae61",
    "upshift_mcp/repository_boundary.py": "e9afe0e2afd88837c63701cdc18a38d66e31565763f48425c9f59d07f11aae32",
    "tests/test_mcp_foundation.py": "a686ef64a158150f052becf775b72ec959476408e1c6d831ed39b4d1b0c9918a",
}

# Phase 11.2 changed exactly one existing file, `.bob/mcp.json`, and added two.
#
# The config change is the interpreter fix. Bob launches `command` from its own
# PATH and does not activate a virtual environment, so a bare `python` reached an
# interpreter without `mcp==2.2.0` and the server never started. The argument is
# now the project-local launcher, which hands off to the project environment.
# The MCP surface is untouched by this: same one server, same one read-only tool,
# same parameters, same STDIO schema. The launcher adds no tool, so the digests
# of `upshift_mcp/*` stay exactly where Phase 11.1 put them.
#
# This entry moves `.bob/mcp.json` out of the Phase 8 set and re-arms the
# tripwire for it and for the new files, so Phase 12+ cannot quietly change them.
PHASE_ELEVEN_TWO_SHA256 = {
    ".bob/mcp.json": "3dfe0fc81f37cf6b821a5c373be05281b730680277eb66c27520ae5577813f90",
    ".bob/mcp_launcher.py": "cf2f3ebb9f5a97b416a7aaa51ebdcbc62c2bc6c1bd9957378f2712dbed77645b",
    "tests/test_bob_mcp_integration.py": "e11bcc15ff77ddfb5980ab76ff534ae37036e2482f8357b5b98698b0f7dba1c7",
    "tests/test_bob_launcher.py": "382d996a20f1a04a07de4cb67ad3ab61e864b8a210035a0b994e356477cd596d",
}


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class StubRollbackProvider:
    """Minimal controlled provider used to drive specific recovery outcomes.

    It implements only the three controlled operations. Like every valid
    provider it has no ``command``, ``args``, or ``callable`` parameter.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        result: Optional[RollbackResult] = None,
        candidate: Optional[VerificationCandidate] = None,
        load_error: Optional[Exception] = None,
    ) -> None:
        self.available = available
        self.result = result
        self.candidate = candidate
        self.load_error = load_error
        self.availability_calls = 0
        self.restore_calls = 0
        self.load_calls = 0
        self.observed_candidates = []

    def rollback_available(self, candidate_id: str) -> bool:
        self.availability_calls += 1
        return self.available

    def restore_baseline(self, candidate_id: str) -> RollbackResult:
        self.restore_calls += 1
        self.observed_candidates.append(candidate_id)
        if self.result is not None:
            return self.result
        return RollbackResult(
            attempted=True,
            succeeded=True,
            reason="stub restored the baseline",
            restored_candidate_id=RESTORED_CANDIDATE_ID,
        )

    def load_restored_baseline(self) -> VerificationCandidate:
        self.load_calls += 1
        if self.load_error is not None:
            raise self.load_error
        if self.candidate is not None:
            return self.candidate
        # Default to the genuine known-good baseline behavior so a spy provider
        # can exercise the successful recovery path without a stub behavior.
        return VerificationCandidate(
            candidate_id=RESTORED_CANDIDATE_ID,
            path="stub",
            resolve=_baseline_resolve,
        )


def _baseline_resolve(user_id: str) -> Any:
    return _real_baseline().resolve(user_id)


def _real_baseline() -> VerificationCandidate:
    global _REAL_BASELINE
    if _REAL_BASELINE is None:
        _REAL_BASELINE = load_candidate(BASELINE_CANDIDATE_ID)
    return _REAL_BASELINE


_REAL_BASELINE: Optional[VerificationCandidate] = None


def _code_only(path: Path) -> str:
    """Return the file's code with comments and string literals removed.

    Prose in docstrings may legitimately *describe* a prohibited capability.
    The guarantee that matters is that no executable code references one.
    """

    code = []
    string_token_types = {tokenize.STRING, tokenize.COMMENT}
    # Python 3.12+ tokenizes f-strings into dedicated FSTRING_* tokens, so the
    # literal prose inside them must be filtered out too.
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        token_type = getattr(tokenize, name, None)
        if token_type is not None:
            string_token_types.add(token_type)
    for token in tokenize.generate_tokens(
        io.StringIO(path.read_text(encoding="utf-8")).readline
    ):
        if token.type in string_token_types:
            continue
        code.append(token.string)
    return " ".join(code)


def _repository_digest() -> str:
    """Hash every tracked source file so repository mutation is detectable."""

    digest = hashlib.sha256()
    roots = ("app", "demo", "tests", "upshift_mcp", "docs", ".bob")
    files = []
    for name in roots:
        files.extend(
            path
            for path in (REPOSITORY_ROOT / name).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts
        )
    files.extend(
        path for path in (REPOSITORY_ROOT / name).iterdir() if path.is_file()
        for name in (".gitignore", "README.md", "requirements.txt")
        if (REPOSITORY_ROOT / name).is_file()
    )
    for path in sorted(set(files)):
        digest.update(path.relative_to(REPOSITORY_ROOT).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migration():
    return MigrationDescription.from_metadata(load_metadata())


@pytest.fixture(scope="module")
def impact_report(migration):
    return ImpactAnalyzer(str(REPOSITORY_ROOT)).analyze(migration)


@pytest.fixture(scope="module")
def risk_report(migration, impact_report):
    return RiskAnalyzer().analyze(migration, impact_report)


@pytest.fixture(scope="module")
def cases():
    return build_cases()


@pytest.fixture(scope="module")
def failing_verification():
    return verify_candidate(REGRESSION_CANDIDATE)


@pytest.fixture(scope="module")
def passing_verification():
    return verify_candidate(CORRECT_CANDIDATE)


def _recover(impact_report, risk_report, verification, provider, cases, *, decision=None):
    decision = decision or MigrationDecisionEngine().decide(
        impact_report, risk_report, verification
    )
    return MigrationRecoveryEngine().recover(
        impact_report, risk_report, verification, decision, provider, cases
    )


# ==========================================================================
# 1. ACCEPT -> COMPLETED
# ==========================================================================


def test_accept_transitions_to_completed(passing_verification, impact_report, risk_report, cases):
    assert passing_verification.status == VERIFICATION_PASS

    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, risk_report, passing_verification, provider, cases
        )

    assert report.initial_decision == DECISION_ACCEPT
    assert report.entry_state == ACCEPTED
    assert report.recovery_state == COMPLETED
    assert report.state_history == (ACCEPTED, COMPLETED)
    assert report.accepted is True
    assert report.rollback_attempted is False
    assert report.rollback_verification_status == NOT_RUN
    assert report.final_candidate_id == CORRECT_CANDIDATE


# ==========================================================================
# 2, 3, 4, 5. FAIL -> ROLLBACK_REQUIRED -> ROLLING_BACK
#    -> ROLLBACK_VERIFICATION -> COMPLETED
# ==========================================================================


def test_failure_enters_rollback_required(failing_verification, impact_report, risk_report, cases):
    assert failing_verification.status == VERIFICATION_FAIL

    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    assert report.initial_decision == DECISION_REJECT
    assert report.entry_state == ROLLBACK_REQUIRED
    assert ROLLBACK_REQUIRED in report.state_history


def test_recovery_rolls_back_then_verifies_then_completes(
    failing_verification, impact_report, risk_report, cases
):
    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    assert report.state_history == (
        ROLLBACK_REQUIRED,
        ROLLING_BACK,
        ROLLBACK_VERIFICATION,
        COMPLETED,
    )
    assert report.recovery_state == COMPLETED
    assert report.rollback_attempted is True
    assert report.rollback_succeeded is True
    assert report.rollback_verification_status == VERIFICATION_PASS
    assert report.final_verification_status == VERIFICATION_PASS
    assert report.final_candidate_id == RESTORED_CANDIDATE_ID


def test_every_required_transition_is_recorded_with_a_reason(
    failing_verification, impact_report, risk_report, cases
):
    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    pairs = [(t.source_state, t.target_state) for t in report.transitions]
    assert pairs == [
        (ROLLBACK_REQUIRED, ROLLING_BACK),
        (ROLLING_BACK, ROLLBACK_VERIFICATION),
        (ROLLBACK_VERIFICATION, COMPLETED),
    ]
    for transition in report.transitions:
        assert transition.reason.strip()
        assert transition.source_state in RECOVERY_STATES
        assert transition.target_state in RECOVERY_STATES


def test_restored_baseline_is_reported_as_recovered_not_accepted(
    failing_verification, impact_report, risk_report, cases
):
    """A recovered migration must never be presented as a successful migration."""

    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    assert report.recovery_state == COMPLETED
    assert report.accepted is False
    assert report.initial_decision == DECISION_REJECT
    assert RECOVERED_BASELINE_NOTE in report.explanations


def test_only_one_rollback_and_one_baseline_verification_occur(
    failing_verification, impact_report, risk_report, cases
):
    provider = StubRollbackProvider()
    report = _recover(impact_report, risk_report, failing_verification, provider, cases)

    assert report.recovery_state == COMPLETED
    assert provider.restore_calls == 1
    assert provider.load_calls == 1


# ==========================================================================
# 6. Failed rollback -> RECOVERY_FAILED
# ==========================================================================


def test_restore_that_fails_ends_in_recovery_failed(
    failing_verification, impact_report, risk_report, cases
):
    provider = StubRollbackProvider(
        result=RollbackResult.failure("the controlled mechanism could not restore")
    )
    report = _recover(impact_report, risk_report, failing_verification, provider, cases)

    assert report.recovery_state == RECOVERY_FAILED
    assert report.state_history == (ROLLBACK_REQUIRED, ROLLING_BACK, RECOVERY_FAILED)
    assert report.rollback_attempted is True
    assert report.rollback_succeeded is False
    assert report.rollback_verification_status == NOT_RUN
    assert report.accepted is False


def test_baseline_verification_failure_ends_in_recovery_failed(
    failing_verification, impact_report, risk_report, cases
):
    """Rollback reported success, but the restored baseline did not verify."""

    provider = StubRollbackProvider(
        candidate=VerificationCandidate(
            candidate_id=RESTORED_CANDIDATE_ID,
            path="stub",
            resolve=lambda user_id: "Wrong value",
        )
    )
    report = _recover(impact_report, risk_report, failing_verification, provider, cases)

    assert report.recovery_state == RECOVERY_FAILED
    assert report.state_history == (
        ROLLBACK_REQUIRED,
        ROLLING_BACK,
        ROLLBACK_VERIFICATION,
        RECOVERY_FAILED,
    )
    assert report.rollback_succeeded is True
    assert report.rollback_verification_status == VERIFICATION_FAIL
    assert report.accepted is False
    assert provider.restore_calls == 1


def test_unloadable_baseline_ends_in_recovery_failed(
    failing_verification, impact_report, risk_report, cases
):
    provider = StubRollbackProvider(load_error=UnknownCandidateError("nothing to load"))
    report = _recover(impact_report, risk_report, failing_verification, provider, cases)

    assert report.recovery_state == RECOVERY_FAILED
    assert report.rollback_verification_status == VERIFICATION_FAIL


def test_report_rejects_a_successful_rollback_without_verified_baseline():
    with pytest.raises(ValueError, match="independently"):
        RecoveryReport(
            migration_name=MIGRATION_NAME,
            initial_candidate_id=REGRESSION_CANDIDATE,
            initial_decision=DECISION_REJECT,
            initial_verification_status=VERIFICATION_FAIL,
            recovery_state=COMPLETED,
            rollback_attempted=True,
            rollback_succeeded=True,
            rollback_verification_status=NOT_RUN,
            final_verification_status=NOT_RUN,
            final_candidate_id=RESTORED_CANDIDATE_ID,
            replanning_required=False,
            explanations=(),
            evidence=(),
            transitions=(
                StateTransition(ROLLBACK_REQUIRED, ROLLING_BACK, "r"),
                StateTransition(ROLLING_BACK, ROLLBACK_VERIFICATION, "r"),
                StateTransition(ROLLBACK_VERIFICATION, COMPLETED, "r"),
            ),
        )


# ==========================================================================
# 7. Rollback unavailable -> REPLANNING -> INCONCLUSIVE
# ==========================================================================


def test_rollback_unavailable_moves_to_replanning_then_inconclusive(
    failing_verification, impact_report, risk_report, cases
):
    with BenchmarkRollbackProvider(available=False) as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    assert report.state_history == (ROLLBACK_REQUIRED, REPLANNING, INCONCLUSIVE)
    assert report.recovery_state == INCONCLUSIVE
    assert report.rollback_attempted is False
    assert report.replanning_required is True
    assert report.replan_request is not None
    assert report.accepted is False
    # Availability was checked, but restoration was never attempted.
    assert provider.restore_calls == 0


# ==========================================================================
# 8. Verification INCONCLUSIVE -> INCONCLUSIVE
# ==========================================================================


def test_inconclusive_verification_stays_inconclusive(
    impact_report, risk_report, passing_verification, cases
):
    inconclusive_cases = (
        VerificationCase(
            case_id="user-001",
            description="no declared expectation",
            inputs=("user-001",),
        ),
    )
    report = _recover(
        impact_report,
        risk_report,
        verify_candidate(CORRECT_CANDIDATE, cases=inconclusive_cases),
        StubRollbackProvider(available=False),
        cases,
    )

    assert report.initial_verification_status == VERIFICATION_INCONCLUSIVE
    assert report.initial_decision == DECISION_INCONCLUSIVE
    assert report.entry_state == INCONCLUSIVE
    assert report.recovery_state == INCONCLUSIVE
    assert report.state_history == (INCONCLUSIVE, INCONCLUSIVE)
    assert report.replanning_required is False
    assert report.rollback_attempted is False
    assert report.accepted is False


# ==========================================================================
# 9. Risk level does not determine recovery correctness
# ==========================================================================


def _risk_with_level(level: str, score: int) -> RiskReport:
    """A synthetic risk report carrying only a level and a score.

    The API strings are deliberately neutral placeholders. The recovery engine
    reads only ``risk_level`` and ``risk_score``, and using real migration
    symbols here would add spurious references to the Phase 4 impact surface.
    """

    return RiskReport(
        migration_name=MIGRATION_NAME,
        old_api="old-entry-point",
        target_api="target-entry-point",
        risk_level=level,
        risk_score=score,
        risk_factors=(),
        impacted_file_count=0,
        direct_old_reference_count=0,
        target_reference_count=0,
        metadata_listed_file_count=0,
        affected_test_files=(),
        renamed_symbol_count=0,
        explanations=("synthetic risk context",),
    )


@pytest.mark.parametrize("level,score", [("LOW", 0), ("MEDIUM", 8), ("HIGH", 266)])
def test_passing_verification_completes_regardless_of_risk_level(
    level, score, impact_report, passing_verification, cases
):
    synthetic = _risk_with_level(level, score)
    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, synthetic, passing_verification, provider, cases
        )

    assert report.recovery_state == COMPLETED
    assert report.accepted is True
    assert any(f"Risk level {level}" in note for note in report.explanations)


@pytest.mark.parametrize("level,score", [("LOW", 0), ("HIGH", 266)])
def test_failing_verification_never_completes_regardless_of_risk_level(
    level, score, impact_report, failing_verification, cases
):
    synthetic = _risk_with_level(level, score)
    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, synthetic, failing_verification, provider, cases
        )

    assert report.initial_decision == DECISION_REJECT
    assert report.accepted is False
    assert report.recovery_state == COMPLETED
    assert report.state_history[0] == ROLLBACK_REQUIRED
    assert any(f"Risk level {level}" in note for note in report.explanations)


# ==========================================================================
# 10. Failed verification cannot become ACCEPTED
# ==========================================================================


def test_failed_verification_with_accept_decision_is_refused(
    failing_verification, impact_report, risk_report, cases
):
    forged = DecisionReport(
        migration_name=MIGRATION_NAME,
        decision=DECISION_ACCEPT,
        verification_status=VERIFICATION_FAIL,
        risk_level=risk_report.risk_level,
        risk_score=risk_report.risk_score,
        total_cases=failing_verification.total_cases,
        passed_cases=failing_verification.passed_cases,
        failed_cases=failing_verification.failed_cases,
        inconclusive_cases=0,
        skipped_cases=0,
        candidate_id=REGRESSION_CANDIDATE,
        explanations=("forged",),
        evidence=(DecisionEvidence(kind="verification", summary="forged"),),
    )

    with BenchmarkRollbackProvider() as provider:
        with pytest.raises(RecoveryInputError, match="did not pass"):
            _recover(
                impact_report,
                risk_report,
                failing_verification,
                provider,
                cases,
                decision=forged,
            )


def test_no_recovery_outcome_reports_accepted_for_a_failed_migration(
    failing_verification, impact_report, risk_report, cases
):
    providers = (
        StubRollbackProvider(),
        StubRollbackProvider(result=RollbackResult.failure("nope")),
        StubRollbackProvider(
            candidate=VerificationCandidate(
                candidate_id=RESTORED_CANDIDATE_ID,
                path="stub",
                resolve=lambda user_id: "wrong",
            )
        ),
        StubRollbackProvider(available=False),
    )
    for provider in providers:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )
        assert report.accepted is False, provider
        assert report.recovery_state != ACCEPTED, provider
        assert report.initial_decision == DECISION_REJECT


# ==========================================================================
# 11. No arbitrary rollback commands
# ==========================================================================


def test_rollback_abstraction_exposes_no_command_surface():
    surface = set(RollbackProvider.__protocol_attrs__)
    assert surface == {
        "rollback_available",
        "restore_baseline",
        "load_restored_baseline",
    }
    # The only parameter anywhere in the contract is an allowlisted candidate id.
    assert {
        name
        for name in surface
        for name in inspect.signature(getattr(RollbackProvider, name)).parameters
        if name != "self"
    } <= {"candidate_id"}


def test_rollback_result_carries_no_executable_payload():
    names = {f.name for f in fields(RollbackResult)}
    assert names == {
        "attempted",
        "succeeded",
        "reason",
        "restored_candidate_id",
        "mechanism",
    }
    for forbidden in ("command", "args", "script", "callable", "path", "code", "source"):
        assert forbidden not in names


def test_recover_signature_accepts_no_command_or_path():
    signature = inspect.signature(MigrationRecoveryEngine.recover)
    names = {
        name
        for name in signature.parameters
        if name not in ("self",)
    }
    assert names == {
        "impact_report",
        "risk_report",
        "verification_report",
        "decision_report",
        "rollback_provider",
        "verification_cases",
    }


def test_recovery_engine_contains_no_execution_primitives():
    """The engine itself is pure orchestration: no execution of any kind."""

    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    for forbidden in (
        "subprocess",
        "socket",
        "urllib",
        "http",
        "httpx",
        "requests",
        "ctypes",
        "pickle",
        "shutil",
        "pathlib",
        "tempfile",
        "os",
    ):
        assert forbidden not in imported, f"recovery engine imports {forbidden}"

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    called |= {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for forbidden in (
        "system",
        "popen",
        "check_output",
        "check_call",
        "run",
        "call",
        "eval",
        "exec",
        "compile",
        "__import__",
        "fork",
        "spawn",
        "unlink",
        "rmtree",
        "remove",
        "rename",
        "write_text",
        "write_bytes",
    ):
        assert forbidden not in called, f"recovery engine calls {forbidden}"


def test_benchmark_provider_only_deletes_its_own_temporary_workspace():
    """The provider removes the temp copy it created, and nothing else.

    ``shutil.rmtree`` is used exactly once, and its sole argument is the
    provider's own workspace handle, which the provider chose itself. No
    caller-supplied path can reach it, so this is not arbitrary filesystem
    deletion.
    """

    source = PROVIDER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    rmtree_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rmtree"
    ]
    assert len(rmtree_calls) == 1
    call = rmtree_calls[0]
    assert isinstance(call.func.value, ast.Name), "rmtree must be shutil.rmtree"
    assert call.func.value.id == "shutil"
    assert isinstance(call.args[0], ast.Attribute), "argument must be an attribute"
    assert call.args[0].attr == "_workspace", "must delete only its own workspace"

    # The workspace is never assigned from a parameter or an external input.
    assignments = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    }
    assert "_workspace" in assignments

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for forbidden in ("subprocess", "socket", "urllib", "ctypes", "pickle", "requests"):
        assert forbidden not in imported, f"provider imports {forbidden}"


def test_benchmark_provider_exposes_no_command_parameters():
    signature = inspect.signature(BenchmarkRollbackProvider.__init__)
    assert [
        name for name in signature.parameters if name != "self"
    ] == ["available"]
    for name, expected in (
        ("rollback_available", ["candidate_id"]),
        ("restore_baseline", ["candidate_id"]),
        ("load_restored_baseline", []),
    ):
        parameters = [
            p.name
            for p in inspect.signature(getattr(BenchmarkRollbackProvider, name)).parameters.values()
            if p.name != "self"
        ]
        assert parameters == expected, name


@pytest.mark.parametrize("path", [ENGINE_PATH, PROVIDER_PATH], ids=["engine", "provider"])
def test_recovery_modules_never_invoke_git(path):
    code = _code_only(path)
    for forbidden in (
        "subprocess",
        "check_output",
        "check_call",
        "Popen",
        "force_push",
        "force-push",
        "reset --hard",
        "git checkout",
        "git reset",
        "git revert",
        "git commit",
        "git clone",
    ):
        assert forbidden not in code, f"{path.name} invokes {forbidden}"


def test_stub_and_benchmark_providers_reject_unknown_candidates():
    with BenchmarkRollbackProvider() as provider:
        with pytest.raises(UnknownCandidateError):
            provider.rollback_available("not_a_candidate")
        with pytest.raises(UnknownCandidateError):
            provider.restore_baseline("../../etc/passwd")
        with pytest.raises(UnknownCandidateError):
            provider.restore_baseline("rm -rf /")


# ==========================================================================
# 12. No repository mutation outside controlled temporary fixtures
# ==========================================================================


def test_full_recovery_cycle_does_not_mutate_the_repository(
    failing_verification, impact_report, risk_report, cases
):
    before = _repository_digest()
    with BenchmarkRollbackProvider() as provider:
        _recover(impact_report, risk_report, failing_verification, provider, cases)
    assert _repository_digest() == before


def test_restoration_never_writes_inside_the_repository():
    with BenchmarkRollbackProvider() as provider:
        provider.restore_baseline(REGRESSION_CANDIDATE)
        candidate = provider.load_restored_baseline()
        workspace = provider._workspace

    # The reported path is a stable label, and it is not a repository path.
    assert candidate.path.startswith(f"{WORKSPACE_KIND}/")
    assert not candidate.path.startswith("demo/")
    assert workspace is not None
    assert REPOSITORY_ROOT not in workspace.parents
    assert not workspace.exists(), "the temporary working copy must be cleaned up"


def test_temporary_working_copy_is_removed_on_close():
    provider = BenchmarkRollbackProvider()
    provider.restore_baseline(REGRESSION_CANDIDATE)
    workspace = provider._workspace
    assert workspace is not None and workspace.is_dir()

    provider.close()
    assert not workspace.exists()
    assert provider._workspace is None


def test_temporary_working_copy_is_removed_on_exception():
    provider = BenchmarkRollbackProvider()
    with pytest.raises(RuntimeError):
        with provider:
            provider.restore_baseline(REGRESSION_CANDIDATE)
            workspace = provider._workspace
            raise RuntimeError("boom")
    assert not workspace.exists()


# ==========================================================================
# 13, 15. Determinism
# ==========================================================================


def test_recovery_report_is_deterministic(failing_verification, impact_report, risk_report, cases):
    reports = []
    for _ in range(3):
        with BenchmarkRollbackProvider() as provider:
            reports.append(
                _recover(impact_report, risk_report, failing_verification, provider, cases)
            )

    first = reports[0]
    for report in reports[1:]:
        assert report == first
        assert report.explanations == first.explanations
        assert report.evidence == first.evidence
        assert report.transitions == first.transitions
        assert report.state_history == first.state_history


def test_report_contains_no_temporary_path_or_clock(failing_verification, impact_report, risk_report, cases):
    with BenchmarkRollbackProvider() as provider:
        provider.restore_baseline(REGRESSION_CANDIDATE)
        workspace_name = provider._workspace.name
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    rendered = " ".join(
        (*report.explanations, *(item.summary for item in report.evidence))
    )
    assert workspace_name not in rendered
    assert "upshift-recovery-" not in rendered
    assert "AppData" not in rendered
    for marker in ("timestamp", "generated_at", "created_at", "uuid"):
        assert marker not in rendered.lower()


def test_state_history_is_deterministic_for_every_path(impact_report, risk_report, cases):
    failing = verify_candidate(REGRESSION_CANDIDATE)
    passing = verify_candidate(CORRECT_CANDIDATE)
    inconclusive = verify_candidate(
        CORRECT_CANDIDATE,
        cases=(
            VerificationCase(case_id="user-001", description="undeclared", inputs=("user-001",)),
        ),
    )

    histories = []
    for verification, available in (
        (passing, True),
        (failing, True),
        (failing, False),
        (inconclusive, True),
    ):
        with BenchmarkRollbackProvider(available=available) as provider:
            report = _recover(
                impact_report, risk_report, verification, provider, cases
            )
        histories.append(report.state_history)

    assert histories == [
        (ACCEPTED, COMPLETED),
        (ROLLBACK_REQUIRED, ROLLING_BACK, ROLLBACK_VERIFICATION, COMPLETED),
        (ROLLBACK_REQUIRED, REPLANNING, INCONCLUSIVE),
        (INCONCLUSIVE, INCONCLUSIVE),
    ]


def test_replan_request_is_deterministic(failing_verification, impact_report, risk_report, cases):
    with BenchmarkRollbackProvider(available=False) as provider:
        first = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )
    with BenchmarkRollbackProvider(available=False) as provider:
        second = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    assert first.replan_request == second.replan_request


# ==========================================================================
# 14. Immutability
# ==========================================================================


@pytest.mark.parametrize(
    "obj,attr,value",
    [
        pytest.param(
            RollbackResult(
                attempted=True,
                succeeded=True,
                reason="r",
                restored_candidate_id=RESTORED_CANDIDATE_ID,
            ),
            "succeeded",
            False,
            id="rollback-result",
        ),
        pytest.param(
            StateTransition(ROLLING_BACK, ROLLBACK_VERIFICATION, "r"),
            "target_state",
            COMPLETED,
            id="state-transition",
        ),
        pytest.param(
            RecoveryEvidence(kind="rollback", summary="s"),
            "summary",
            "tampered",
            id="recovery-evidence",
        ),
        pytest.param(
            ReplanRequest(
                migration_name=MIGRATION_NAME,
                candidate_id=REGRESSION_CANDIDATE,
                failed_verification_cases=("user-002",),
                risk_evidence=(),
                impact_evidence=(),
                rollback_result="unavailable",
                baseline_verification_status=NOT_RUN,
                recommended_investigation_areas=("a",),
            ),
            "status",
            "approved",
            id="replan-request",
        ),
    ],
)
def test_value_types_are_frozen(obj, attr, value):
    with pytest.raises(FrozenInstanceError):
        setattr(obj, attr, value)


def test_recovery_report_is_frozen(failing_verification, impact_report, risk_report, cases):
    with BenchmarkRollbackProvider() as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    with pytest.raises(FrozenInstanceError):
        report.recovery_state = ACCEPTED
    with pytest.raises(FrozenInstanceError):
        report.explanations = ("tampered",)
    with pytest.raises(FrozenInstanceError):
        report.transitions = ()


def test_replan_request_cannot_be_promoted_to_an_action():
    with pytest.raises(ValueError, match="proposal"):
        ReplanRequest(
            migration_name=MIGRATION_NAME,
            candidate_id=REGRESSION_CANDIDATE,
            failed_verification_cases=(),
            risk_evidence=(),
            impact_evidence=(),
            rollback_result="unavailable",
            baseline_verification_status=NOT_RUN,
            recommended_investigation_areas=(),
            status="approved",
        )


def test_state_transition_rejects_unknown_states():
    with pytest.raises(ValueError, match="source_state"):
        StateTransition("MAYBE", COMPLETED, "r")
    with pytest.raises(ValueError, match="target_state"):
        StateTransition(ACCEPTED, "MAYBE", "r")


# ==========================================================================
# 16. Re-plan output is a proposal only
# ==========================================================================


def test_replan_output_is_a_proposal_only(failing_verification, impact_report, risk_report, cases):
    with BenchmarkRollbackProvider(available=False) as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )

    request = report.replan_request
    assert request is not None
    assert request.status == "proposal"
    assert request.migration_name == MIGRATION_NAME
    assert request.candidate_id == REGRESSION_CANDIDATE
    assert request.failed_verification_cases == ("user-002",)
    assert request.baseline_verification_status == NOT_RUN
    assert request.risk_evidence and request.impact_evidence
    assert request.recommended_investigation_areas

    # It carries evidence and suggestions, never an instruction to act.
    rendered = " ".join(request.recommended_investigation_areas).lower()
    for forbidden in ("run the migration", "execute", "apply now", "retry now", "commit"):
        assert forbidden not in rendered


def test_replanning_does_not_modify_the_repository(
    failing_verification, impact_report, risk_report, cases
):
    before = _repository_digest()
    with BenchmarkRollbackProvider(available=False) as provider:
        report = _recover(
            impact_report, risk_report, failing_verification, provider, cases
        )
    assert report.replan_request is not None
    assert _repository_digest() == before


# ==========================================================================
# 17. No autonomous retry loop
# ==========================================================================


def test_no_autonomous_retry_loop_runs(failing_verification, impact_report, risk_report, cases):
    """Recovery is a fixed sequence: one restore, one baseline verification."""

    provider = StubRollbackProvider()
    report = _recover(impact_report, risk_report, failing_verification, provider, cases)

    assert report.recovery_state == COMPLETED
    assert provider.availability_calls == 1
    assert provider.restore_calls == 1
    assert provider.load_calls == 1
    assert provider.observed_candidates == [REGRESSION_CANDIDATE]


def test_failure_path_also_attempts_rollback_exactly_once(
    failing_verification, impact_report, risk_report, cases
):
    provider = StubRollbackProvider(result=RollbackResult.failure("nope"))
    report = _recover(impact_report, risk_report, failing_verification, provider, cases)

    assert report.recovery_state == RECOVERY_FAILED
    assert provider.restore_calls == 1
    assert provider.load_calls == 0


def test_engine_source_contains_no_loop_or_recursion():
    """Structural guarantee: recovery cannot loop or retry itself."""

    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.While, ast.AsyncFor))
    ], "recovery engine must not contain a loop"

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                target = inner.func
                name = (
                    target.id
                    if isinstance(target, ast.Name)
                    else target.attr
                    if isinstance(target, ast.Attribute)
                    else None
                )
                assert name != node.name, f"{node.name} calls itself"


def test_no_timer_or_scheduling_primitive_is_used():
    for path in (ENGINE_PATH, PROVIDER_PATH):
        code = _code_only(path)
        for forbidden in (
            "sleep",
            "while True",
            "schedule",
            "cron",
            "retry",
            "backoff",
            "asyncio",
            "threading",
            "Timer",
        ):
            assert forbidden not in code, f"{path.name} uses {forbidden}"


# ==========================================================================
# 18. Existing Phase 4-8 behavior remains unchanged
# ==========================================================================


@pytest.mark.parametrize("relative,expected", sorted(PROTECTED_SHA256.items()))
def test_protected_phase_four_to_seven_files_are_unchanged(relative, expected):
    digest = hashlib.sha256((REPOSITORY_ROOT / relative).read_bytes()).hexdigest()
    assert digest == expected, f"{relative} was modified by Phase 9"


@pytest.mark.parametrize("relative,expected", sorted(PHASE_EIGHT_SHA256.items()))
def test_phase_eight_mcp_behavior_is_preserved(relative, expected):
    digest = hashlib.sha256((REPOSITORY_ROOT / relative).read_bytes()).hexdigest()
    assert digest == expected, f"{relative} was modified by Phase 9"


@pytest.mark.parametrize(
    "relative,expected", sorted(PHASE_ELEVEN_ONE_SHA256.items())
)
def test_phase_eleven_one_bridge_files_are_unchanged(relative, expected):
    digest = hashlib.sha256((REPOSITORY_ROOT / relative).read_bytes()).hexdigest()
    assert digest == expected, f"{relative} was modified after Phase 11.1"


@pytest.mark.parametrize(
    "relative,expected", sorted(PHASE_ELEVEN_TWO_SHA256.items())
)
def test_phase_eleven_two_launcher_files_are_unchanged(relative, expected):
    digest = hashlib.sha256((REPOSITORY_ROOT / relative).read_bytes()).hexdigest()
    assert digest == expected, f"{relative} was modified after Phase 11.2"


def test_existing_benchmark_outcomes_are_unchanged():
    assert verify_candidate(BASELINE_CANDIDATE_ID).status == VERIFICATION_PASS
    assert verify_candidate(CORRECT_CANDIDATE).status == VERIFICATION_PASS
    assert verify_candidate(REGRESSION_CANDIDATE).status == VERIFICATION_FAIL


def test_recovery_module_does_not_reimplement_existing_analyses():
    """Recovery coordinates evidence; it must not recompute it."""

    source = ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)

    for forbidden in (
        "ImpactAnalyzer",
        "RiskAnalyzer",
        "MigrationDecisionEngine",
        "MigrationDescription",
        "RiskReport",
        "ImpactReport",
    ):
        assert forbidden not in imported, f"recovery engine imports {forbidden}"


def test_mcp_tool_surface_is_unchanged_by_recovery():
    """Phase 9 must not add an MCP tool or expose rollback over MCP."""

    server = (REPOSITORY_ROOT / "upshift_mcp" / "server.py").read_text(encoding="utf-8")
    assert "rollback" not in server.lower()
    assert "recovery" not in server.lower()

    import asyncio

    from upshift_mcp import server as mcp_server

    tools = asyncio.run(mcp_server.server.list_tools())
    assert [tool.name for tool in tools] == ["get_migration_context"]


def test_phase_nine_added_no_new_migration_references():
    """Phase 9 must not enlarge the Phase 4 impact surface.

    The impact analyzer is a textual scanner, so a new file that mentions a
    migration symbol would silently change the Phase 4 evidence numbers. These
    are the values recorded through Phase 8.
    """

    from app.verification.profile_label_benchmark import load_metadata

    migration = MigrationDescription.from_metadata(load_metadata())
    report = ImpactAnalyzer(str(REPOSITORY_ROOT)).analyze(migration)

    assert len(report.impacted_files) == 18
    assert len(report.direct_references) == 112
    assert len(report.target_references) == 116


def test_phase_nine_did_not_change_the_risk_score():
    from app.verification.profile_label_benchmark import load_metadata

    migration = MigrationDescription.from_metadata(load_metadata())
    impact = ImpactAnalyzer(str(REPOSITORY_ROOT)).analyze(migration)
    risk = RiskAnalyzer().analyze(migration, impact)

    assert (risk.risk_level, risk.risk_score) == ("HIGH", 266)


# ==========================================================================
# Input validation
# ==========================================================================


def test_mismatched_candidate_ids_are_refused(
    failing_verification, impact_report, risk_report, cases
):
    decision = MigrationDecisionEngine().decide(impact_report, risk_report, failing_verification)
    forged = DecisionReport(
        migration_name=MIGRATION_NAME,
        decision=DECISION_REJECT,
        verification_status=VERIFICATION_FAIL,
        risk_level=decision.risk_level,
        risk_score=decision.risk_score,
        total_cases=5,
        passed_cases=4,
        failed_cases=1,
        inconclusive_cases=0,
        skipped_cases=0,
        candidate_id=CORRECT_CANDIDATE,
        explanations=(),
        evidence=(),
    )
    with pytest.raises(RecoveryInputError, match="same candidate"):
        _recover(
            impact_report,
            risk_report,
            failing_verification,
            StubRollbackProvider(),
            cases,
            decision=forged,
        )


def test_provider_without_the_controlled_surface_is_refused(
    failing_verification, impact_report, risk_report, cases
):
    class NotAProvider:
        def rollback_available(self, candidate_id):  # pragma: no cover - rejected
            return True

    with pytest.raises(RecoveryInputError, match="controlled restoration"):
        _recover(
            impact_report,
            risk_report,
            failing_verification,
            NotAProvider(),
            cases,
        )


def test_missing_cases_prevent_an_unverified_completion(
    failing_verification, impact_report, risk_report, cases
):
    report = _recover(
        impact_report, risk_report, failing_verification, StubRollbackProvider(), ()
    )
    assert report.recovery_state == RECOVERY_FAILED
    assert report.rollback_verification_status == VERIFICATION_FAIL
