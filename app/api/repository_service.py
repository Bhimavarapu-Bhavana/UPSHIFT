"""Application service for UPSHIFT real-repository mode.

Real-repository mode is the second input to the *existing* UPSHIFT engines. It
adds no analysis of its own. It calls the same engines, once each, in the same
order as the controlled-benchmark path, and hands their untouched reports to the
same presentation model:

    Dashboard -> this service -> app.repository.RealRepositoryInput  (read only)
                           -> bounded_snapshot  (UPSHIFT's own scratch copy)
                           -> ImpactAnalyzer (Phase 4, unchanged)
                           -> RiskAnalyzer (Phase 5)
                           -> CandidateVerifier (Phase 6)
                           -> MigrationDecisionEngine (Phase 7)
                           -> MigrationRecoveryEngine (Phase 9)

The one thing that genuinely differs is verification, and it differs for a
reason rather than by choice. UPSHIFT never executes a repository, so it cannot
observe a repository's behaviour and cannot declare preserved behavior for it.
That is reported honestly: the existing Phase 6 verifier is run over cases
built from the discovered references with no expected behavior attached, every
case comes back ``INCONCLUSIVE``, and the existing Phase 7 decision engine
therefore reports ``INCONCLUSIVE``. A repository UPSHIFT only reads is never
reported as accepted.

Security boundary enforced here:

* A repository path is accepted only through the operator-configured boundary in
  :mod:`app.security.repository_input`. There is no default boundary, so real
  mode is closed until an operator allows a root.
* A migration declaration is validated into identifier-only symbols. It cannot
  name a file to read, a module to import, or a command to run.
* The verification candidate is an in-process function defined in this module.
  It is never called, because every case carries no expected behavior; a test
  asserts that. It is never built from repository content.
* The rollback provider reports that no controlled baseline exists, so the
  existing recovery engine can never restore an analyzed repository. In this
  mode it does not need to: verification is INCONCLUSIVE by design, and the
  engine already records that neither acceptance nor rollback is justified.
* The analyzed repository is opened read-only and is never written. The frozen
  Phase 4 analyzer reads a bounded copy in UPSHIFT's own scratch directory,
  which is removed before this function returns, including on failure.
* No shell, subprocess, Git, or network operation is performed, nothing is
  written to the analyzed repository, and the result is a plain frozen value.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from app.api.dashboard_result import (
    MODE_REAL_REPOSITORY,
    DashboardResult,
    build_dashboard_result,
)
from app.core.decision_engine import MigrationDecisionEngine
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.recovery_engine import MigrationRecoveryEngine
from app.core.risk_analyzer import RiskAnalyzer
from app.repository.real_input import (
    NO_CONTROLLED_ROLLBACK,
    NoControlledRollbackProvider,
    RealRepositoryInput,
)
from app.repository.snapshot import bounded_snapshot
from app.security.repository_input import (
    BOUNDARY_POLICY,
    MODE_DEMO,
    RepositoryBoundary,
    RepositoryInputError,
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
    "REAL_REPOSITORY_BOUNDARY",
    "REAL_REPOSITORY_CANDIDATE_ID",
    "REAL_REPOSITORY_REVIEW_CASE_ID",
    "RepositoryInputError",
    "analyze_repository",
    "describe_analysis_modes",
    "real_repository_cases",
]

#: The candidate label real-repository mode reports. It is not a migration
#: candidate; it names the static review the engines actually performed.
REAL_REPOSITORY_CANDIDATE_ID = "real_repository"

#: Case id used when the repository contained none of the declared symbols, so
#: there is nothing to review and nothing that could be declared preserved.
REAL_REPOSITORY_REVIEW_CASE_ID = "static_review:no_symbol_reference_discovered"

REAL_REPOSITORY_BOUNDARY = (
    "Real-repository mode reads an operator-bounded local repository without "
    "executing it, then reports the existing UPSHIFT evidence. It is static "
    "evidence only: no behavioral verification is possible without running "
    "repository code, so the decision is reported as INCONCLUSIVE."
)

NO_CONTROLLED_ROLLBACK_REASON = (
    "A repository UPSHIFT only reads has no controlled baseline. Nothing in it "
    "is changed, so there is nothing to restore: the existing recovery engine "
    "reports that no acceptance and no rollback are justified."
)


def _refuse_execution(*inputs: Any) -> Any:
    """Placeholder observation that is never reached.

    Real-repository mode cannot observe behaviour, so every verification case
    carries no expected behavior and the existing verifier records
    ``INCONCLUSIVE`` before it would call a resolver. This function exists so
    that, if that invariant were ever broken, the run fails loudly as a
    verification failure instead of silently observing a repository.

    It is a module-level function over its arguments. It takes no path, no
    module, and no command, and it never touches the repository.
    """

    raise RepositoryInputError(
        "real-repository mode must never execute or observe repository code"
    )


def real_repository_cases(impact_report: Any) -> Tuple[VerificationCase, ...]:
    """Build static-review cases from the discovered references.

    The cases are derived entirely from what the existing Phase 4 analyzer
    found, so the verification section reports the same symbols the impact
    section does. No expectation is attached, because a repository UPSHIFT only
    reads has no declared preserved behavior to compare against.

    Args:
        impact_report: An existing Phase 4 :class:`ImpactReport`.

    Returns:
        Deterministic cases, one per discovered symbol, each with
        ``expected`` left as ``UNAVAILABLE``.
    """

    symbols = sorted(
        {reference.symbol for reference in impact_report.direct_references}
        | {reference.symbol for reference in impact_report.target_references}
    )

    if not symbols:
        return (
            VerificationCase(
                case_id=REAL_REPOSITORY_REVIEW_CASE_ID,
                description=(
                    "No reference to a declared migration symbol was found in the "
                    "files that were read, so there is no call site to review."
                ),
                inputs=(),
                expected=UNAVAILABLE,
                required=True,
            ),
        )

    return tuple(
        VerificationCase(
            case_id=f"static_review:{symbol}",
            description=(
                f"Behavioral preservation for {symbol!r} cannot be established "
                "without executing repository code."
            ),
            inputs=(),
            expected=UNAVAILABLE,
            required=True,
        )
        for symbol in symbols
    )


def _migration_description(declaration: Any) -> MigrationDescription:
    """Map a validated declaration onto the existing Phase 4 description.

    The vocabulary comes from the frozen Phase 4 engine rather than from this
    module, by routing the declaration through the same
    ``MigrationDescription.from_metadata`` call the controlled benchmark uses.
    That is why an entry point alone is enough, and why a real repository and
    the benchmark can never search with two different symbol rules.
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


def analyze_repository(
    repository_path: Any,
    migration: Any,
    allowed_repository_roots: Optional[Sequence[Any]] = None,
) -> DashboardResult:
    """Read one real repository and run the existing UPSHIFT pipeline on it.

    Args:
        repository_path: An absolute local directory path. It is accepted only
            when it resolves inside an operator-allowed boundary root.
        migration: The migration declaration: a name, the old and target entry
            points, and optionally the symbols and renamed pairs involved.
        allowed_repository_roots: The boundary roots the operator configured.

    Returns:
        A :class:`DashboardResult` in ``real_repository`` mode carrying the
        existing impact, risk, verification, decision, and recovery reports plus
        the repository evidence describing what was read.

    Raises:
        RepositoryInputError: If the boundary is not configured, the path is not
            an allowed existing directory, or the migration declaration is
            malformed. No message echoes the refused path or declaration.
    """

    boundary = build_repository_boundary(allowed_repository_roots)
    root = boundary.resolve(repository_path)
    migration_description = _migration_description(migration)

    repository_input = RealRepositoryInput(root)
    load = repository_input.load()

    # Phase 4 discovers the relevant files and references from the bounded read
    # alone, so the analyzer can only ever see content the reader admitted: no
    # second walk, no refused file, no budget overrun. The scratch copy is
    # UPSHIFT's own and is removed before this function returns.
    with bounded_snapshot(load.files) as snapshot_root:
        impact_report = ImpactAnalyzer(snapshot_root).analyze(migration_description)

    risk_report = RiskAnalyzer().analyze(migration_description, impact_report)

    cases = real_repository_cases(impact_report)
    verification_report = CandidateVerifier().verify(
        VerificationCandidate(
            candidate_id=REAL_REPOSITORY_CANDIDATE_ID,
            path=load.evidence.repository_path,
            resolve=_refuse_execution,
        ),
        cases,
        migration_name=migration_description.name,
    )
    decision_report = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )
    recovery_report = MigrationRecoveryEngine().recover(
        impact_report,
        risk_report,
        verification_report,
        decision_report,
        NoControlledRollbackProvider(),
        cases,
    )

    return build_dashboard_result(
        impact_report,
        risk_report,
        verification_report,
        decision_report,
        recovery_report,
        mode=MODE_REAL_REPOSITORY,
        repository_evidence=load.evidence,
        rollback_mechanism=NO_CONTROLLED_ROLLBACK,
    )


def describe_analysis_modes(
    allowed_repository_roots: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Describe both analysis modes, and why one of them may be unavailable.

    Returns:
        A JSON-serializable mapping naming the active modes. Real-repository
        mode reports whether an operator boundary is configured, and never
        discloses a filesystem path.
    """

    try:
        boundary: RepositoryBoundary = build_repository_boundary(allowed_repository_roots)
        reason = (
            "An operator-allowed repository boundary is configured, so a local "
            "repository can be read inside that boundary."
            if boundary.is_configured
            else (
                "Real-repository mode is unavailable: no repository boundary is "
                "configured. Start the dashboard with an explicit allowed "
                "repository root to enable it."
            )
        )
    except RepositoryInputError as error:
        boundary = RepositoryBoundary()
        reason = f"Real-repository mode is unavailable: {error}"

    return {
        "active_mode": MODE_DEMO,
        "demo": {
            "id": MODE_DEMO,
            "label": "DEMO MODE",
            "available": True,
            "summary": (
                "Controlled benchmark candidates, analyzed by the same engines. "
                "Deterministic, and unchanged by real-repository mode."
            ),
        },
        "real_repository": {
            "id": MODE_REAL_REPOSITORY,
            "label": "REAL REPOSITORY MODE",
            "available": boundary.is_configured,
            "summary": REAL_REPOSITORY_BOUNDARY,
            "reason": reason,
            "no_baseline_note": NO_CONTROLLED_ROLLBACK_REASON,
            "boundary_policy": BOUNDARY_POLICY,
            "boundary": list(boundary.describe()),
            "reads_repository_files": True,
            "executes_repository_code": False,
            "migration_declaration_required": True,
        },
    }
