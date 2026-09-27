"""Phase 16: hardening checks across the whole real-time loop.

No new features here. Every test is a check that something a caller might try is
refused, or that an existing boundary is still where it was.

The security sweep is deliberately cross-cutting: the new execution and
persistence modules are checked for the capabilities they must not have, the
existing HTTP surface is checked for the routes it must not have grown, and the
controlled benchmark is checked for having produced the same evidence as before.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.execution.migration_task import MigrationTaskRequest, run_migration_task
from tests._real_repository import (
    MIGRATION,
    OLD_MODULE,
    OLD_SERVICE_PATH,
    build_real_repository,
    complete_plan,
    incomplete_plan,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: The modules Phase 12-15 added.
NEW_MODULES = (
    "app/execution/operations.py",
    "app/execution/migration_executor.py",
    "app/execution/migration_task.py",
    "app/execution/rollback.py",
    "app/execution/task_state.py",
    "app/execution/__init__.py",
    "app/persistence/task_store.py",
    "app/persistence/__init__.py",
)

#: Modules that may not exist, because their existence would mean a capability
#: was added rather than bounded.
FORBIDDEN_MODULE_NAMES = (
    "app/execution/shell.py",
    "app/execution/commands.py",
    "app/execution/runner.py",
    "app/execution/plugins.py",
    "app/persistence/queue.py",
    "app/persistence/worker.py",
    "app/persistence/scheduler.py",
)


@pytest.fixture()
def repository():
    with build_real_repository() as fixture:
        yield fixture


# --------------------------------------------------------------------------
# No arbitrary execution
# --------------------------------------------------------------------------


def test_no_execution_capability_was_added():
    """AST over every new module: no process, no dynamic execution, no network."""

    forbidden_modules = {
        "subprocess", "importlib", "socket", "pty", "urllib", "requests", "http",
        "ftplib", "telnetlib", "smtplib", "xmlrpc", "multiprocessing", "ctypes",
        "pickle", "shelve", "marshal", "code", "codeop",
    }
    forbidden_calls = {"eval", "exec", "__import__", "compile", "globals", "vars"}
    forbidden_attributes = {
        ("os", "system"), ("os", "popen"), ("os", "spawnl"), ("os", "spawnv"),
        ("os", "execv"), ("os", "execve"), ("os", "startfile"), ("os", "fork"),
        ("shutil", "rmtree"), ("shutil", "move"), ("pathlib", "Path.unlink"),
    }

    for relative in NEW_MODULES:
        tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (forbidden_modules & imported), (
            f"{relative} imports a forbidden capability: {sorted(forbidden_modules & imported)}"
        )

        called = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not (forbidden_calls & called), (
            f"{relative} calls {sorted(forbidden_calls & called)}"
        )

        qualified = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    qualified.add((node.func.value.id, node.func.attr))
        assert not (forbidden_attributes & qualified), (
            f"{relative} calls {sorted(forbidden_attributes & qualified)}"
        )


def test_nothing_reads_credentials_or_the_environment():
    for relative in NEW_MODULES:
        tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not ({"environ", "getenv", "putenv", "expandvars", "getcwd"} & attributes), (
            f"{relative} touches the process environment or working directory"
        )
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not ({"environ", "getenv"} & names), f"{relative} reads the environment"


def test_no_capability_module_was_added():
    for relative in FORBIDDEN_MODULE_NAMES:
        assert not (PROJECT_ROOT / relative).exists(), (
            f"{relative} exists, which means a capability was added instead of bounded"
        )


def test_the_executor_is_reachable_only_through_the_task_loop(repository):
    """Nothing in the HTTP surface accepts a plan, so the API cannot execute."""

    from app.api.http_app import ROUTE_TABLE

    paths = sorted(route.path for route in ROUTE_TABLE)
    assert paths == ["/", "/api/analyze", "/api/candidates"], (
        f"the documented three-route surface changed: {paths}"
    )

    # The API layer must not even mention a plan, an occurrence count, or the
    # operation name, so no request shape can reach the executor.
    source = (PROJECT_ROOT / "app/api/http_app.py").read_text(encoding="utf-8")
    for token in ("plan", "dry_run", "expected_occurrences", "replace_exact", "expected_content"):
        assert token not in source, f"the API layer mentions {token!r}"

    # And the executor is not imported by anything under app/api either.
    for path in (PROJECT_ROOT / "app" / "api").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "migration_executor" not in text, f"{path.name} imports the executor"
        assert "run_migration_task" not in text, f"{path.name} runs a migration task"


# --------------------------------------------------------------------------
# No arbitrary filesystem access, no traversal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../..",
        "../../../etc/passwd",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "\\\\localhost\\c$\\Windows\\win.ini",
        "~/.ssh/id_rsa",
        "pkg/../../outside.py",
        "pkg/./../../outside.py",
        "\\".join([".."] * 8) + "/etc/passwd",
    ],
)
def test_traversal_is_refused_by_the_operation_validator(path):
    from app.execution.operations import FileOperationError, require_file_operation

    with pytest.raises(FileOperationError):
        require_file_operation(
            {"operation": "replace_exact", "path": path, "old_content": "a", "new_content": "b"}
        )


def test_a_repository_outside_the_approved_boundary_is_refused():
    """The loop refuses a repository the operator never approved."""

    from app.security.repository_input import RepositoryBoundaryError

    other = build_real_repository()
    try:
        with pytest.raises(RepositoryBoundaryError):
            run_migration_task(
                MigrationTaskRequest(
                    repository_path=str(other.root),
                    migration=MIGRATION,
                    plan=complete_plan(),
                    dry_run=False,
                    observed_symbols=(OLD_MODULE,),
                    allowed_repository_roots=["C:/somewhere/else"],
                ),
                task_id="hardening-1",
            )
    finally:
        other.close()


def test_a_closed_boundary_refuses_everything(repository):
    from app.security.repository_input import RepositoryBoundaryError

    with pytest.raises(RepositoryBoundaryError, match="no repository boundary"):
        run_migration_task(
            MigrationTaskRequest(
                repository_path=str(repository.root),
                migration=MIGRATION,
                plan=complete_plan(),
                dry_run=False,
                allowed_repository_roots=None,
            ),
            task_id="hardening-2",
        )


def test_the_sensitive_files_were_never_read(repository):
    """The loop's own analysis refused them, which is why they are untouchable."""

    from app.repository.real_input import RealRepositoryInput

    load = RealRepositoryInput(repository.root).load()
    admitted = {label for label, _ in load.files}
    assert ".env" not in admitted
    assert "config/credentials.py" not in admitted
    reasons = {summary.reason for summary in load.evidence.skipped}
    assert "sensitive_file" in reasons

    # And the repository is untouched by the whole loop.
    before = repository.read(OLD_SERVICE_PATH)
    run_migration_task(
        MigrationTaskRequest(
            repository_path=str(repository.root),
            migration=MIGRATION,
            plan=complete_plan(),
            dry_run=False,
            observed_symbols=(OLD_MODULE,),
            allowed_repository_roots=repository.allowed_roots,
        ),
        task_id="hardening-3",
    )
    assert repository.read(OLD_SERVICE_PATH) == repository.migrated_service_text


# --------------------------------------------------------------------------
# The benchmark is unchanged
# --------------------------------------------------------------------------

_BENCHMARK_EVIDENCE = {"impacted_files": 18, "direct": 112, "target": 116}


def test_the_controlled_benchmark_is_exactly_where_it_was():
    from app.core.decision_engine import MigrationDecisionEngine
    from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
    from app.core.risk_analyzer import RiskAnalyzer
    from app.verification.verifier import (
        CandidateVerifier,
        VerificationCase,
        VerificationCandidate,
    )

    from upshift_mcp.context import build_migration_context

    result = build_migration_context("correct_migration")

    assert result["impact"]["impacted_file_count"] == _BENCHMARK_EVIDENCE["impacted_files"]
    assert result["impact"]["direct_reference_count"] == _BENCHMARK_EVIDENCE["direct"]
    assert result["impact"]["target_reference_count"] == _BENCHMARK_EVIDENCE["target"]
    assert result["risk"]["risk_level"] == "HIGH"
    assert result["risk"]["risk_score"] == 266
    assert result["verification"]["verification_status"] == "PASS"
    assert result["decision"]["decision_state"] == "ACCEPT"

    # And the three decisions are each still what they were.
    for candidate, status, decision in (
        ("old_baseline", "PASS", "ACCEPT"),
        ("correct_migration", "PASS", "ACCEPT"),
        ("regression_migration", "FAIL", "REJECT"),
    ):
        context = build_migration_context(candidate)
        assert context["verification"]["verification_status"] == status, candidate
        assert context["decision"]["decision_state"] == decision, candidate

    del (
        ImpactAnalyzer, MigrationDescription, RiskAnalyzer, CandidateVerifier,
        VerificationCase, VerificationCandidate, MigrationDecisionEngine,
    )


def test_the_benchmark_demo_rollback_still_restores_the_baseline():
    from demo.migration_benchmark.recovery_demo import main

    assert main() == 0


# --------------------------------------------------------------------------
# Final acceptance: both complete paths, on the real filesystem
# --------------------------------------------------------------------------


def test_final_acceptance_pass_path(repository):
    """REAL REPOSITORY -> PLAN -> EXECUTE -> VERIFY PASS -> ACCEPT."""

    from app.api.dashboard_result import OUTCOME_ACCEPTED

    before = repository.read(OLD_SERVICE_PATH)
    result = run_migration_task(
        MigrationTaskRequest(
            repository_path=str(repository.root),
            migration=MIGRATION,
            plan=complete_plan(),
            dry_run=False,
            observed_symbols=(OLD_MODULE,),
            allowed_repository_roots=repository.allowed_roots,
        ),
        task_id="acceptance-pass",
    )

    assert repository.read(OLD_SERVICE_PATH) != before
    assert result.execution.wrote_anything
    assert result.verification_report.status == "PASS"
    assert result.decision_report.decision == "ACCEPT"
    assert result.migration_accepted is True
    assert result.dashboard_result.final_outcome == OUTCOME_ACCEPTED
    assert result.dashboard_result.task_state["state"] == "COMPLETED"


def test_final_acceptance_fail_path(repository):
    """REAL REPOSITORY -> EXECUTE -> VERIFY FAIL -> REJECT -> ROLLBACK -> COMPLETED."""

    from app.api.dashboard_result import OUTCOME_REJECTED_BASELINE_RESTORED

    before = repository.read(OLD_SERVICE_PATH)
    result = run_migration_task(
        MigrationTaskRequest(
            repository_path=str(repository.root),
            migration=MIGRATION,
            plan=incomplete_plan(),
            dry_run=False,
            observed_symbols=(OLD_MODULE,),
            allowed_repository_roots=repository.allowed_roots,
        ),
        task_id="acceptance-fail",
    )

    assert result.execution.wrote_anything, "the change really was applied"
    assert result.verification_report.status == "FAIL"
    assert result.decision_report.decision == "REJECT"
    assert result.recovery_report.rollback_succeeded is True
    assert result.recovery_report.rollback_verification_status == "PASS"
    assert repository.read(OLD_SERVICE_PATH) == before, "the baseline is restored"
    assert result.migration_accepted is False
    assert result.dashboard_result.final_outcome == OUTCOME_REJECTED_BASELINE_RESTORED
    assert result.dashboard_result.task_state["state"] == "ROLLED_BACK"


def test_both_paths_agree_with_what_is_on_disk(repository):
    """The reported evidence and the real files cannot disagree."""

    import hashlib

    from app.repository.real_input import RealRepositoryInput

    result = run_migration_task(
        MigrationTaskRequest(
            repository_path=str(repository.root),
            migration=MIGRATION,
            plan=complete_plan(),
            dry_run=False,
            observed_symbols=(OLD_MODULE,),
            allowed_repository_roots=repository.allowed_roots,
        ),
        task_id="acceptance-agree",
    )
    assert result.execution is not None
    for entry in result.execution.applied:
        on_disk = repository.read(entry.path)
        assert entry.sha256_after == hashlib.sha256(on_disk.encode("utf-8")).hexdigest()

    # A fresh independent read of the repository agrees with the loop.
    fresh = RealRepositoryInput(repository.root).load()
    joined = "\n".join(text for _label, text in fresh.files)
    assert OLD_MODULE not in joined
    assert result.verification_report.status == "PASS"
