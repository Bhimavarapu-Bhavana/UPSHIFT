"""Application service behind the UPSHIFT dashboard.

This is the only new orchestration added in Phase 10.1, and it contains no
analysis of its own. It calls the existing UPSHIFT engines once each, in the
existing order, and hands their untouched reports to the presentation model:

    Dashboard -> this service -> ImpactAnalyzer (Phase 4)
                           -> RiskAnalyzer (Phase 5)
                           -> CandidateVerifier (Phase 6)
                           -> MigrationDecisionEngine (Phase 7)
                           -> MigrationRecoveryEngine (Phase 9)

Security boundary enforced here:

* The only caller-supplied value is a candidate identifier, validated against
  the static allowlist in :mod:`app.security.candidates` before any engine
  runs. There is no parameter for a path, a module, a URL, or a command.
* No shell, subprocess, or dynamic code execution is performed by this module.
* The controlled rollback provider is constructed internally and always closed,
  so a request can never leave a temporary working copy behind and can never
  supply its own provider.
* Nothing is written and no Git state is modified.
* The result is a plain frozen value with no engine or provider handle on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from app.api.dashboard_result import (
    MODE_DEMO,
    MODE_REAL_REPOSITORY,
    DashboardResult,
    build_dashboard_result,
)
from app.core.decision_engine import MigrationDecisionEngine
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.recovery_engine import MigrationRecoveryEngine
from app.core.risk_analyzer import RiskAnalyzer
from app.security.candidates import (
    PROHIBITED_DASHBOARD_CAPABILITIES,
    known_candidate_ids,
    require_controlled_candidate,
)
from app.verification.benchmark_rollback import BenchmarkRollbackProvider
from app.verification.profile_label_benchmark import build_cases, verify_candidate
from app.verification.verifier import UnknownCandidateError

__all__ = [
    "CONTROLLED_BASELINE_MECHANISM",
    "DASHBOARD_SERVICE_BOUNDARY",
    "PROHIBITED_DASHBOARD_CAPABILITIES",
    "UnknownCandidateError",
    "analyze_candidate",
    "controlled_candidate_ids",
    "describe_service",
]

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The restoration mechanism the controlled benchmark actually has. Reported as
#: a fact next to the real-repository mechanism, which is none, so the dashboard
#: never implies both modes can restore a baseline.
CONTROLLED_BASELINE_MECHANISM = "controlled_benchmark_baseline"

DASHBOARD_SERVICE_BOUNDARY = (
    "Presentation-only service that calls the existing UPSHIFT engines over the "
    "controlled benchmark. It adds no analysis and executes no migration."
)


def controlled_candidate_ids() -> Tuple[str, ...]:
    """Return the candidate identifiers this service will accept."""

    return known_candidate_ids()


def describe_service() -> Dict[str, Any]:
    """Return the self-description the dashboard presents about itself."""

    return {
        "service": "UPSHIFT dashboard service",
        "boundary": DASHBOARD_SERVICE_BOUNDARY,
        "read_only_evidence": True,
        "executes_migration": False,
        "controlled_candidates": list(known_candidate_ids()),
        "prohibited_capabilities": list(PROHIBITED_DASHBOARD_CAPABILITIES),
        "engines": [
            "Phase 4 impact analyzer",
            "Phase 5 risk analyzer",
            "Phase 6 verification engine",
            "Phase 7 decision engine",
            "Phase 9 recovery engine",
        ],
        "modes": [MODE_DEMO, MODE_REAL_REPOSITORY],
    }


def analyze_candidate(candidate_id: Any) -> DashboardResult:
    """Run the existing UPSHIFT pipeline for one controlled candidate.

    Args:
        candidate_id: One of the statically allowlisted candidate identifiers.

    Returns:
        A :class:`DashboardResult` projecting the impact, risk, verification,
        decision, and recovery reports produced by the existing engines.

    Raises:
        UnknownCandidateError: If ``candidate_id`` is not an allowlisted
            identifier, or carries path or command syntax.
    """

    requested = require_controlled_candidate(candidate_id)

    metadata = _load_metadata()
    migration = MigrationDescription.from_metadata(metadata)
    migration_name = migration.name

    impact_report = ImpactAnalyzer(_REPOSITORY_ROOT).analyze(migration)
    risk_report = RiskAnalyzer().analyze(migration, impact_report)

    cases = build_cases(metadata)
    verification_report = verify_candidate(
        requested, cases=cases, migration_name=migration_name
    )
    decision_report = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )

    with BenchmarkRollbackProvider() as provider:
        recovery_report = MigrationRecoveryEngine().recover(
            impact_report,
            risk_report,
            verification_report,
            decision_report,
            provider,
            cases,
        )

    return build_dashboard_result(
        impact_report,
        risk_report,
        verification_report,
        decision_report,
        recovery_report,
        mode=MODE_DEMO,
        rollback_mechanism=CONTROLLED_BASELINE_MECHANISM,
    )


def _load_metadata() -> Dict[str, Any]:
    from app.verification.profile_label_benchmark import load_metadata

    return load_metadata()
