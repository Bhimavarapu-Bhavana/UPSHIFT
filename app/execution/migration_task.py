"""The real-time loop: one task, walked from an approved repository to an outcome.

    ANALYZING -> PLANNING -> EXECUTING -> VERIFYING -> COMPLETED
                                 |            |
                                 |            +-> REJECT -> RECOVERING -> ROLLED_BACK
                                 +-> dry run only, nothing written

This module orchestrates. It adds no analysis, no verification, and no
recovery logic of its own:

* **ANALYZING** reuses the existing bounded reader
  (:class:`app.repository.real_input.RealRepositoryInput`) and the frozen Phase 4
  and Phase 5 engines.
* **PLANNING** / **EXECUTING** are the new :mod:`app.execution.operations` and
  :mod:`app.execution.migration_executor`.
* **VERIFYING** reuses the existing Phase 6 verifier, over a candidate that
  *reads* the changed files instead of running repository code.
* **DECIDING** reuses the existing Phase 7 decision engine.
* **RECOVERING** reuses the existing Phase 9 recovery engine unchanged, with the
  baseline provider from :mod:`app.execution.rollback`. The engine already
  performs at most one restore and at most one baseline verification, so this
  module adds no retry of its own.

Every state in the returned journal was reached by a real call. There is no
simulated progress and no predicted state: if a step did not run, its state was
not recorded. The one exception is deliberate and is not progress: a dry run
records ``COMPLETED`` when the preview finished, having written nothing.

How verification is real without executing the repository
---------------------------------------------------------
UPSHIFT never runs repository code, so a verification case cannot observe
behaviour by calling into the target. Instead the candidate *reads* the admitted
files and the cases assert facts about their current content:

* one case per changed file, expecting the exact text the executor read back
  from disk after writing;
* one case per observed symbol, expecting **zero** remaining references across
  the whole admitted set.

The second kind is what makes an incomplete migration fail honestly. A plan that
rewrites one call site and leaves another leaves a non-zero count in a real
file, and the existing Phase 6 verifier reports ``FAIL`` from the filesystem. No
failure is scripted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from app.api.dashboard_result import MODE_REAL_REPOSITORY, build_dashboard_result
from app.core.decision_engine import MigrationDecisionEngine
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.recovery_engine import MigrationRecoveryEngine
from app.core.risk_analyzer import RiskAnalyzer
from app.execution.migration_executor import (
    RESTORE_MECHANISM,
    ExecutionResult,
    MigrationExecutor,
)
from app.execution.operations import FileOperation, require_file_operations
from app.execution.rollback import BaselineRollbackProvider
from app.execution.task_state import (
    ANALYZING,
    COMPLETED,
    EXECUTING,
    PLANNING,
    RECOVERING,
    ROLLED_BACK,
    VERIFYING,
    TaskJournal,
)
from app.repository.real_input import NoControlledRollbackProvider, RealRepositoryInput
from app.repository.snapshot import bounded_snapshot
from app.security.repository_input import (
    build_repository_boundary,
    require_migration_declaration,
)
from app.verification.verifier import (
    UNAVAILABLE,
    CandidateVerifier,
    VerificationCandidate,
    VerificationCase,
)

__all__ = [
    "MigrationTaskRequest",
    "MigrationTaskResult",
    "read_text_reader",
    "run_migration_task",
    "symbol_reference_reader",
]

#: Case-id prefix for a case observed after execution.
MIGRATION_CASE_PREFIX = "migration_applied"

#: Case-id prefix for the dry-run placeholder.
DRY_RUN_CASE_ID = "migration_applied:dry_run"

_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class MigrationTaskRequest:
    """Everything one task needs, and nothing it may not have.

    Attributes:
        repository_path: Absolute local directory, accepted only inside an
            operator-approved root.
        migration: The identifier-only declaration, validated by the existing
            :func:`require_migration_declaration`.
        plan: The structured operations, required unless ``dry_run``.
        dry_run: When true nothing is written and no baseline is captured.
        observed_symbols: Module-level names whose remaining reference count is
            verified after execution. A bare identifier, never a dotted path.
        allowed_repository_roots: The operator's approved roots.
    """

    repository_path: Any
    migration: Any
    plan: Optional[Sequence[Any]] = None
    dry_run: bool = True
    observed_symbols: Tuple[str, ...] = ()
    allowed_repository_roots: Optional[Sequence[Any]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "dry_run", bool(self.dry_run))
        for symbol in self.observed_symbols:
            if not isinstance(symbol, str) or not _IDENTIFIER.match(symbol):
                raise ValueError(
                    "observed_symbols accepts only bare module-level identifiers"
                )
        object.__setattr__(
            self, "observed_symbols", tuple(sorted(set(self.observed_symbols)))
        )


@dataclass(frozen=True)
class MigrationTaskResult:
    """The outcome of one task, with the evidence that produced it."""

    journal: TaskJournal
    dashboard_result: Any
    execution: Optional[ExecutionResult]
    impact_report: Any
    risk_report: Any
    verification_report: Any
    decision_report: Any
    recovery_report: Any
    rollback_provider: Optional[BaselineRollbackProvider]

    @property
    def task_id(self) -> str:
        """The journal's task identifier."""

        return self.journal.task_id

    @property
    def state(self) -> str:
        """The last state actually reached."""

        return self.journal.state

    @property
    def migration_accepted(self) -> bool:
        """True only when the existing decision engine accepted the migration.

        Read from the decision report, never from the task's terminal state, so
        a completed rollback cannot make this true.
        """

        return self.decision_report.decision == "ACCEPT"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable projection of the whole task."""

        return {
            "task_id": self.task_id,
            "state": self.state,
            "history": list(self.journal.history()),
            "migration_accepted": self.migration_accepted,
            "execution": self.execution.to_dict() if self.execution else None,
            "decision": self.decision_report.decision,
            "verification_status": self.verification_report.status,
            "recovery_state": self.recovery_report.recovery_state,
            "rollback_succeeded": self.recovery_report.rollback_succeeded,
            "events": [event.to_dict() for event in self.journal.events()],
        }


# --------------------------------------------------------------------------
# Narrow, read-only observations
# --------------------------------------------------------------------------


def read_text_reader(root: Path, admitted: Sequence[str]):
    """Return ``(read_text, readable)`` over the admitted files only.

    ``read_text`` takes a repository-relative label from ``readable`` and
    returns that file's current text, or ``None``. It cannot be pointed at any
    other label, and it never imports or executes anything.
    """

    root = Path(root).resolve()
    allowed = frozenset(str(label) for label in admitted)

    def read_text(relative_path: Any) -> Optional[str]:
        if not isinstance(relative_path, str) or relative_path not in allowed:
            return None
        parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            return None
        target = (root / Path(*parts)).resolve()
        if target != root and root not in target.parents:
            return None
        try:
            with target.open("r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError):
            return None

    return read_text, tuple(sorted(allowed))


def symbol_reference_reader(read_text, admitted: Sequence[str]):
    """Return a resolver counting one symbol's references across admitted files.

    The count is a property of the real repository: each admitted file is read
    and each line tested for the symbol as a whole word. ``(?<![A-Za-z0-9_])``
    and ``(?![A-Za-z0-9_])`` mean ``legacy_profile`` counts in
    ``legacy_profile.resolve_profile`` and in ``import legacy_profile``, but not inside
    ``my_legacy_profile`` or ``legacy_profiles``.
    """

    def count(symbol: Any) -> int:
        if not isinstance(symbol, str) or not _IDENTIFIER.match(symbol):
            return 0
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])"
        )
        total = 0
        for relative_path in admitted:
            text = read_text(relative_path)
            if not text:
                continue
            total += sum(1 for line in text.splitlines() if pattern.search(line))
        return total

    return count


def _reading_candidate(candidate_id: str, path: str, read_text, count) -> VerificationCandidate:
    """Build a real verification candidate that reads instead of executing.

    The existing verifier calls ``resolve(*inputs)`` and requires a genuine
    :class:`~app.verification.verifier.VerificationCandidate`, so this is that
    type with a narrow resolver. A case's input is a tagged pair, which makes the
    observation unambiguous: ``("file", relative_path)`` returns that file's
    current text and ``("symbol", name)`` returns its reference count. Neither
    observation imports or executes repository code.
    """

    def resolve(*inputs: Any) -> Any:
        if len(inputs) != 1:
            return ""
        tag, value = inputs[0]
        if tag == "file":
            text = read_text(value)
            return "" if text is None else text
        if tag == "symbol":
            return count(value)
        return ""

    return VerificationCandidate(candidate_id=candidate_id, path=path, resolve=resolve)


# --------------------------------------------------------------------------
# Case construction
# --------------------------------------------------------------------------


def _post_execution_cases(
    observed_symbols: Sequence[str], expected_after: Dict[str, str]
) -> Tuple[VerificationCase, ...]:
    """Build post-execution cases from the real post-execution filesystem state."""

    cases = [
        VerificationCase(
            case_id=f"{MIGRATION_CASE_PREFIX}:{path}",
            description=(
                f"After execution, {path} must contain exactly the content the "
                "executor wrote, read back from the filesystem."
            ),
            inputs=(("file", path),),
            expected=expected_after[path],
            required=True,
        )
        for path in sorted(expected_after)
    ]
    cases.extend(
        VerificationCase(
            case_id=f"{MIGRATION_CASE_PREFIX}:no_remaining_reference:{symbol}",
            description=(
                f"No admitted file may still reference {symbol!r} after a "
                "complete migration; its reference count must be 0."
            ),
            inputs=(("symbol", symbol),),
            expected=0,
            required=True,
        )
        for symbol in sorted(observed_symbols)
    )
    if not cases:
        return (
            VerificationCase(
                case_id=f"{MIGRATION_CASE_PREFIX}:no_change",
                description=(
                    "The plan changed no file, so no preserved behavior is claimed."
                ),
                inputs=(("file", ""),),
                expected=UNAVAILABLE,
                required=True,
            ),
        )
    return tuple(cases)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def run_migration_task(
    request: MigrationTaskRequest,
    *,
    task_id: str = "task-1",
) -> MigrationTaskResult:
    """Walk one approved repository through the full real-time loop.

    Args:
        request: The approved repository, the migration declaration, and either
            a plan or a dry-run request.
        task_id: Identifier recorded in the journal.

    Returns:
        A :class:`MigrationTaskResult` carrying the journal, the dashboard
        result, the execution evidence, and the five existing reports.

    Raises:
        RepositoryInputError: If the boundary, the declaration, or the plan is
            refused. A refusal before any step is raised without inventing a
            state; a refusal *during* a step is recorded as ``FAILED`` and
            re-raised, so a crashed step is never mistaken for a finished one.
    """

    journal = TaskJournal(task_id=task_id)

    # Validate everything that can be validated without touching the repository,
    # so a malformed request never appears to have started a run.
    boundary = build_repository_boundary(request.allowed_repository_roots)
    root = boundary.resolve(request.repository_path)
    description = _migration_description(request.migration)
    operations: Optional[Tuple[FileOperation, ...]] = None
    if request.dry_run:
        if request.plan is not None:
            operations = require_file_operations(request.plan)
    elif request.plan is None:
        raise ValueError("an executing task must carry a plan; use dry_run to preview")
    else:
        operations = require_file_operations(request.plan)

    def analyze():
        load = RealRepositoryInput(root).load()
        with bounded_snapshot(load.files) as snapshot_root:
            impact = ImpactAnalyzer(snapshot_root).analyze(description)
        return load, impact, RiskAnalyzer().analyze(description, impact)

    load, impact_report, risk_report = journal.run(
        ANALYZING, "bounded_read_and_analysis", analyze
    )
    admitted = tuple(label for label, _text in load.files)
    read_text, readable = read_text_reader(root, admitted)
    count_symbols = symbol_reference_reader(read_text, readable)

    if request.dry_run:
        return _dry_run(
            journal=journal,
            root=root,
            description=description,
            operations=operations,
            load=load,
            impact=impact_report,
            risk=risk_report,
            read_text=read_text,
            readable=readable,
        )

    assert operations is not None  # guaranteed by the validation above

    provider = BaselineRollbackProvider(root)

    def execute():
        # The executor validates the whole plan against the live repository and
        # refuses before writing if any precondition fails. A refusal raises out
        # of ``run``, which records FAILED and re-raises.
        return MigrationExecutor(root, admitted, backup=provider.capture).execute(
            list(operations)
        )

    journal.record(PLANNING, "plan_validated")
    execution = journal.run(EXECUTING, "bounded_execution", execute)
    provider.record_applied(execution.applied)

    # The expectation is read back from disk, never taken from the plan.
    expected_after = {}
    for entry in execution.applied:
        text = read_text(entry.path)
        if text is not None:
            expected_after[entry.path] = text

    def verify():
        candidate = _reading_candidate(
            "real_repository", root.name, read_text, count_symbols
        )
        report = CandidateVerifier().verify(
            candidate,
            _post_execution_cases(request.observed_symbols, expected_after),
            migration_name=description.name,
        )
        return report

    verification_report = journal.run(
        VERIFYING, "independent_verification", verify
    )
    decision_report = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )

    if decision_report.decision == "ACCEPT":
        journal.record(COMPLETED, "migration_accepted")
        return _assemble(
            journal=journal,
            root=root,
            description=description,
            load=load,
            impact=impact_report,
            risk=risk_report,
            execution=execution,
            verification=verification_report,
            decision=decision_report,
            provider=provider,
        )

    def recover():
        # The existing engine performs at most one restore and at most one
        # baseline verification, and the baseline cases assert each file's exact
        # pre-execution content.
        return MigrationRecoveryEngine().recover(
            impact_report,
            risk_report,
            verification_report,
            decision_report,
            provider,
            provider.baseline_cases(),
        )

    recovery_report = journal.run(RECOVERING, "recovery_engine", recover)
    journal.record(
        ROLLED_BACK if recovery_report.rollback_succeeded else COMPLETED,
        "baseline_restored" if recovery_report.rollback_succeeded else "recovery_incomplete",
    )
    return _assemble(
        journal=journal,
        root=root,
        description=description,
        load=load,
        impact=impact_report,
        risk=risk_report,
        execution=execution,
        verification=verification_report,
        decision=decision_report,
        provider=provider,
        recovery=recovery_report,
    )


def _dry_run(
    *,
    journal: TaskJournal,
    root: Path,
    description: MigrationDescription,
    operations: Optional[Tuple[FileOperation, ...]],
    load: Any,
    impact: Any,
    risk: Any,
    read_text,
    readable: Sequence[str],
) -> MigrationTaskResult:
    """Answer a preview request from the live filesystem, writing nothing."""

    def preview():
        if not operations:
            return None
        return MigrationExecutor(root, readable).dry_run(list(operations))

    journal.record(PLANNING, "dry_run_preview")
    execution = journal.run(PLANNING, "dry_run_evaluated", preview)

    def verify():
        # A dry run changed nothing, so no preserved behavior can be verified.
        # Every case therefore carries no expectation and the existing verifier
        # reports INCONCLUSIVE, which keeps a preview from ever reporting
        # accepted.
        candidate = _reading_candidate(
            "real_repository", root.name, read_text, lambda _symbol: 0
        )
        cases = (
            VerificationCase(
                case_id=DRY_RUN_CASE_ID,
                description=(
                    "A dry run writes nothing, so no preserved behavior can be "
                    "verified against the filesystem."
                ),
                inputs=(("file", ""),),
                expected=UNAVAILABLE,
                required=True,
            ),
        )
        return CandidateVerifier().verify(candidate, cases, migration_name=description.name)

    verification_report = journal.run(VERIFYING, "no_change_to_verify", verify)
    decision_report = MigrationDecisionEngine().decide(impact, risk, verification_report)
    recovery_report = MigrationRecoveryEngine().recover(
        impact, risk, verification_report, decision_report, NoControlledRollbackProvider(), ()
    )
    journal.record(COMPLETED, "dry_run_finished")
    return _assemble(
        journal=journal,
        root=root,
        description=description,
        load=load,
        impact=impact,
        risk=risk,
        execution=execution,
        verification=verification_report,
        decision=decision_report,
        provider=None,
        recovery=recovery_report,
    )


def _assemble(
    *,
    journal: TaskJournal,
    root: Path,
    description: MigrationDescription,
    load: Any,
    impact: Any,
    risk: Any,
    execution: Optional[ExecutionResult],
    verification: Any,
    decision: Any,
    provider: Optional[BaselineRollbackProvider],
    recovery: Any = None,
) -> MigrationTaskResult:
    """Project the loop's own evidence into the existing dashboard model."""

    if recovery is None:
        # An accepted migration has no recovery step. The existing dashboard
        # model requires a recovery report, so the engine is asked what
        # recovery means for an accepted migration rather than a placeholder
        # being invented here.
        recovery = MigrationRecoveryEngine().recover(
            impact, risk, verification, decision, NoControlledRollbackProvider(), ()
        )
    dashboard = build_dashboard_result(
        impact,
        risk,
        verification,
        decision,
        recovery,
        mode=MODE_REAL_REPOSITORY,
        repository_evidence=load.evidence,
        rollback_mechanism=(
            provider.mechanism if provider is not None else RESTORE_MECHANISM
        ),
        # The dashboard's live state is the journal of states this run actually
        # reached. Nothing is projected, predicted, or smoothed here.
        task_state=journal.to_dict(),
    )
    return MigrationTaskResult(
        journal=journal,
        dashboard_result=dashboard,
        execution=execution,
        impact_report=impact,
        risk_report=risk,
        verification_report=verification,
        decision_report=decision,
        recovery_report=recovery,
        rollback_provider=provider,
    )


def _migration_description(declaration: Any) -> MigrationDescription:
    """Map a validated declaration onto the frozen Phase 4 description.

    Same routing as :func:`app.api.repository_service._migration_description`, so
    a real repository and the controlled benchmark can never search with two
    different symbol rules.
    """

    validated = require_migration_declaration(declaration)
    base = MigrationDescription.from_metadata(
        {
            "benchmark": validated.name,
            "old_contract": {
                "entry_point": validated.old_api,
                "fields": list(validated.old_symbols),
            },
            "target_contract": {
                "entry_point": validated.target_api,
                "fields": list(validated.target_symbols),
            },
        }
    )
    if not validated.renamed_symbols:
        return base
    return MigrationDescription(
        name=base.name,
        old_api=base.old_api,
        target_api=base.target_api,
        old_symbols=base.old_symbols,
        target_symbols=base.target_symbols,
        renamed_symbols=validated.renamed_symbols,
    )
