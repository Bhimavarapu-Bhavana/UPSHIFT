"""Read-only migration evidence assembly for the UPSHIFT MCP foundation.

This module projects the existing Phase 4-7 evidence into a single structured,
JSON-serializable mapping. It adds no new analysis and invents no migration
results: every value is copied from an existing UPSHIFT report object.

Security boundary enforced here:

* Benchmark mode takes one caller-supplied value, a candidate identifier, which
  must appear in the static benchmark allowlist. There is no parameter for a
  filesystem path, a module path, or a command.
* Real-repository mode takes an approved repository *name* and an
  identifier-only migration declaration. It never takes a path: the directory
  itself comes from the operator approval in
  :mod:`upshift_mcp.repository_boundary`, and the selected root is re-checked
  against that boundary on its resolved form.
* Both modes are projections of the existing engines. Real-repository mode
  delegates to :func:`app.api.repository_service.analyze_repository`, so the
  reader, the boundary check, and every report are the ones the dashboard uses.
* No shell, subprocess, or dynamic code execution is performed.
* No network request is made and no credentials, secrets, or ``.env`` files are
  read.
* Nothing is written; no repository file, and no Git state, is modified.
* Migration execution, rollback, recovery, and replanning are not implemented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.api.repository_service import analyze_repository
from app.core.decision_engine import MigrationDecisionEngine
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.risk_analyzer import RiskAnalyzer
from app.security.repository_input import RepositoryDeclarationError
from app.verification.profile_label_benchmark import (
    KNOWN_CANDIDATES,
    UnknownCandidateError,
    load_metadata,
    verify_candidate,
)

from .repository_boundary import (
    MCP_REPOSITORY_POLICY,
    mcp_repository_boundary,
    select_approved_root,
)

__all__ = [
    "DEFAULT_CANDIDATE_ID",
    "MIGRATION_CONTEXT_BOUNDARY",
    "PROHIBITED_CAPABILITIES",
    "REAL_REPOSITORY_LIMITATIONS",
    "REPOSITORY_ROOT",
    "UnknownCandidateError",
    "build_migration_context",
    "build_real_repository_context",
    "known_candidate_ids",
]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE_ID = "correct_migration"

_KNOWN_CANDIDATE_IDS: Tuple[str, ...] = tuple(
    spec.candidate_id for spec in KNOWN_CANDIDATES
)

MIGRATION_CONTEXT_BOUNDARY = (
    "Read-only projection of UPSHIFT impact, risk, verification, and decision "
    "evidence over the controlled benchmark. No migration is executed."
)

PROHIBITED_CAPABILITIES: Tuple[str, ...] = (
    "migration execution",
    "repository file modification",
    "shell or subprocess execution",
    "dynamic python execution",
    "arbitrary filesystem paths",
    "credential or secret access",
    ".env access",
    "network requests",
    "git state modification",
    "IBM Bob invocation",
    "rollback",
    "recovery",
    "replanning",
)

#: Stated on every real-repository result so a reader never has to infer the
#: limits of the evidence from the decision state alone.
REAL_REPOSITORY_LIMITATIONS: Tuple[str, ...] = (
    "Static evidence only. The repository is read, never imported or run, so "
    "UPSHIFT cannot observe behaviour and never declares a real repository "
    "accepted.",
    "Verification cases carry no expected behavior, so the existing verifier "
    "reports INCONCLUSIVE and the decision engine reports INCONCLUSIVE.",
    "No controlled baseline is captured for a repository UPSHIFT only reads, "
    "so the recovery engine cannot roll one back and does not attempt to.",
    "The repository is opened read-only. UPSHIFT proposes nothing here; the "
    "migration agent owns every change.",
    "The evidence describes the repository as it was at read time. Call the "
    "tool again after an external change to observe the new state.",
)


def known_candidate_ids() -> Tuple[str, ...]:
    """Return the statically allowlisted benchmark candidate identifiers."""

    return _KNOWN_CANDIDATE_IDS


def build_migration_context(
    candidate_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the read-only migration context for one allowlisted candidate.

    Args:
        candidate_id: One of the statically known benchmark candidate
            identifiers. Arbitrary values are rejected before any evidence is
            gathered, so an unknown identifier cannot reach the analyzers.

    Returns:
        A JSON-serializable mapping describing the migration name, impacted
        files, direct and target references, risk level and score, verification
        status and counts, decision state, and the supporting evidence and
        explanations.

    Raises:
        UnknownCandidateError: If ``candidate_id`` is not allowlisted.
    """

    requested = DEFAULT_CANDIDATE_ID if candidate_id is None else candidate_id
    _require_known_candidate(requested)

    metadata = load_metadata()
    migration = MigrationDescription.from_metadata(metadata)
    migration_name = migration.name

    impact_report = ImpactAnalyzer(REPOSITORY_ROOT).analyze(migration)
    risk_report = RiskAnalyzer().analyze(migration, impact_report)
    verification_report = verify_candidate(
        requested, migration_name=migration_name
    )
    decision_report = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )

    return {
        "migration_name": migration_name,
        "candidate_id": verification_report.candidate_id,
        "candidate_path": verification_report.candidate_path,
        "old_api": impact_report.old_api,
        "target_api": impact_report.target_api,
        "read_only": True,
        "boundary": MIGRATION_CONTEXT_BOUNDARY,
        "impact": {
            "impacted_files": [
                {
                    "path": file_impact.path,
                    "reasons": list(file_impact.reasons),
                    "metadata_listed": file_impact.metadata_listed,
                    "old_symbols": list(file_impact.old_symbols),
                    "target_symbols": list(file_impact.target_symbols),
                }
                for file_impact in impact_report.impacted_files
            ],
            "impacted_file_count": len(impact_report.impacted_files),
            "direct_references": [
                _reference(reference) for reference in impact_report.direct_references
            ],
            "direct_reference_count": len(impact_report.direct_references),
            "target_references": [
                _reference(reference) for reference in impact_report.target_references
            ],
            "target_reference_count": len(impact_report.target_references),
            "metadata_listed_files": list(impact_report.metadata_listed_files),
        },
        "risk": {
            "risk_level": risk_report.risk_level,
            "risk_score": risk_report.risk_score,
            "impacted_file_count": risk_report.impacted_file_count,
            "direct_old_reference_count": risk_report.direct_old_reference_count,
            "target_reference_count": risk_report.target_reference_count,
            "metadata_listed_file_count": risk_report.metadata_listed_file_count,
            "affected_test_files": list(risk_report.affected_test_files),
            "renamed_symbol_count": risk_report.renamed_symbol_count,
            "risk_factors": [
                {
                    "category": factor.category,
                    "severity": factor.severity,
                    "score": factor.score,
                    "evidence": factor.evidence,
                    "reason": factor.reason,
                    "affected_paths": list(factor.affected_paths),
                    "affected_symbols": list(factor.affected_symbols),
                }
                for factor in risk_report.risk_factors
            ],
            "explanations": list(risk_report.explanations),
        },
        "verification": {
            "verification_status": verification_report.status,
            "total_cases": verification_report.total_cases,
            "passed_cases": verification_report.passed_cases,
            "failed_cases": verification_report.failed_cases,
            "inconclusive_cases": verification_report.inconclusive_cases,
            "skipped_cases": verification_report.skipped_cases,
            "failure_evidence": list(verification_report.failure_evidence),
            "results": [
                {
                    "case_id": result.case_id,
                    "description": result.description,
                    "expected": result.expected,
                    "observed": result.observed,
                    "status": result.status,
                    "evidence": result.evidence,
                    "required": result.required,
                }
                for result in verification_report.results
            ],
        },
        "decision": {
            "decision_state": decision_report.decision,
            "accepted": decision_report.accepted,
            "verification_status": decision_report.verification_status,
            "risk_level": decision_report.risk_level,
            "risk_score": decision_report.risk_score,
            "candidate_id": decision_report.candidate_id,
            "total_cases": decision_report.total_cases,
            "passed_cases": decision_report.passed_cases,
            "failed_cases": decision_report.failed_cases,
            "inconclusive_cases": decision_report.inconclusive_cases,
            "skipped_cases": decision_report.skipped_cases,
            "explanations": list(decision_report.explanations),
            "evidence": [
                {
                    "kind": evidence.kind,
                    "summary": evidence.summary,
                    "details": list(evidence.details),
                }
                for evidence in decision_report.evidence
            ],
        },
    }


def _require_known_candidate(candidate_id: str) -> None:
    if not isinstance(candidate_id, str) or candidate_id not in _KNOWN_CANDIDATE_IDS:
        known = ", ".join(sorted(_KNOWN_CANDIDATE_IDS))
        raise UnknownCandidateError(
            f"unknown candidate {candidate_id!r}; known candidates: {known}"
        )


def build_real_repository_context(
    repository: Optional[str] = None,
    migration: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble real-repository evidence from an operator-approved directory.

    This is a projection, not a second pipeline. It calls
    :func:`app.api.repository_service.analyze_repository`, so the reader, the
    boundary check, the bounded snapshot, and every impact, risk, verification,
    decision, and recovery report are the existing ones the dashboard already
    produces for the same directory.

    Args:
        repository: The *name* of an operator-approved repository, never a path.
            ``None`` is accepted only when the operator approved exactly one
            root.
        migration: The migration declaration: a name, the old and target entry
            points, and optionally the symbols and renamed pairs involved. The
            existing declaration validator reduces every field to
            identifier-like symbols before any file is read.

    Returns:
        A JSON-serializable mapping in ``real_repository`` mode carrying the
        repository read evidence, impacted files, references, risk, verification,
        decision, recovery, the approved-root names, and the stated limitations.

    Raises:
        RepositoryInputError: If no boundary is approved, the name is not an
            approved name, or the declaration is malformed. The message never
            echoes the refused name or declaration.
    """

    if migration is None:
        raise RepositoryDeclarationError(
            "real-repository mode requires a migration declaration naming the "
            "migration being prepared"
        )

    boundary = mcp_repository_boundary()
    root = select_approved_root(boundary, repository)

    result = analyze_repository(
        str(root),
        migration,
        [str(allowed) for allowed in boundary.roots],
    )

    return {
        **result.to_dict(),
        "read_only": True,
        "boundary": MCP_REPOSITORY_POLICY,
        "approved_repositories": sorted(root.name for root in boundary.roots),
        "limitations": list(REAL_REPOSITORY_LIMITATIONS),
    }


def _reference(reference: Any) -> Dict[str, Any]:
    return {
        "path": reference.path,
        "symbol": reference.symbol,
        "line": reference.line,
        "column": reference.column,
    }
