"""Deterministic controlled-rollback demonstration for the migration benchmark.

This module wires the existing UPSHIFT components to the controlled benchmark
rollback provider so the recovery path can be observed end to end:

1. the OLD baseline passes,
2. a correct migration is accepted,
3. an intentionally regressed migration fails independent verification and
   triggers recovery,
4. the known-good baseline is restored and passes independent verification
   again, and recovery finishes in ``COMPLETED``.

The demonstration is deterministic: it contains no randomness, no clock, no
network, and no repository mutation. Restoration happens in a temporary working
copy, so the protected OLD baseline is only ever read.

Run it directly to print the recorded sequence:

    python -m demo.migration_benchmark.recovery_demo
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from app.core.decision_engine import MigrationDecisionEngine
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
from app.core.recovery_engine import (
    COMPLETED,
    MigrationRecoveryEngine,
    RecoveryReport,
)
from app.core.risk_analyzer import RiskAnalyzer
from app.verification.benchmark_rollback import (
    RESTORED_CANDIDATE_ID,
    BenchmarkRollbackProvider,
)
from app.verification.profile_label_benchmark import (
    build_cases,
    load_metadata,
    verify_candidate,
)
from app.verification.verifier import CandidateVerifier

__all__ = [
    "BASELINE_STAGE",
    "CORRECT_STAGE",
    "REGRESSION_STAGE",
    "RESTORATION_STAGE",
    "RecoveryDemoStep",
    "recover_candidate",
    "run_recovery_demonstration",
]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

BASELINE_STAGE = "old_baseline"
CORRECT_STAGE = "correct_migration"
REGRESSION_STAGE = "regression_migration"
RESTORATION_STAGE = "restored_baseline"

#: The behavior case the regressed candidate is known to break.
PROBE_USER_ID = "user-002"
PROBE_EXPECTED_LABEL = "Amazing Grace"


@dataclass(frozen=True)
class RecoveryDemoStep:
    """One recorded stage of the demonstration."""

    stage: str
    outcome: str
    detail: str

    def render(self) -> str:
        return f"[{self.stage}] {self.outcome}: {self.detail}"


def _evidence() -> Tuple[Any, Any]:
    """Produce the real Phase 4 and Phase 5 reports for the benchmark."""

    migration = MigrationDescription.from_metadata(load_metadata())
    impact = ImpactAnalyzer(str(REPOSITORY_ROOT)).analyze(migration)
    risk = RiskAnalyzer().analyze(migration, impact)
    return impact, risk


def recover_candidate(
    candidate_id: str = REGRESSION_STAGE,
    *,
    rollback_available: bool = True,
) -> RecoveryReport:
    """Run verification, decision, and recovery for one benchmark candidate."""

    impact, risk = _evidence()
    cases = build_cases()
    verification = verify_candidate(candidate_id)
    decision = MigrationDecisionEngine().decide(impact, risk, verification)

    with BenchmarkRollbackProvider(available=rollback_available) as provider:
        report = MigrationRecoveryEngine().recover(
            impact, risk, verification, decision, provider, cases
        )
    return report


def run_recovery_demonstration() -> Tuple[RecoveryDemoStep, ...]:
    """Return the deterministic four-stage rollback demonstration."""

    impact, risk = _evidence()
    cases = build_cases()
    verifier = MigrationDecisionEngine()
    recovery = MigrationRecoveryEngine()
    steps = []

    # 1. The OLD baseline must pass before any migration is considered.
    baseline = verify_candidate(BASELINE_STAGE)
    steps.append(
        RecoveryDemoStep(
            stage=BASELINE_STAGE,
            outcome=baseline.status,
            detail=(
                f"the known-good baseline passed {baseline.passed_cases} of "
                f"{baseline.total_cases} behavior case(s) before recovery"
            ),
        )
    )

    # 2. A correct migration needs no recovery.
    correct = verify_candidate(CORRECT_STAGE)
    correct_decision = verifier.decide(impact, risk, correct)
    with BenchmarkRollbackProvider() as provider:
        correct_report = recovery.recover(
            impact, risk, correct, correct_decision, provider, cases
        )
    steps.append(
        RecoveryDemoStep(
            stage=CORRECT_STAGE,
            outcome=correct_report.recovery_state,
            detail=(
                f"decision {correct_decision.decision} on verification "
                f"{correct.status}; state path "
                f"{' -> '.join(correct_report.state_history)}; no rollback needed"
            ),
        )
    )

    # 3 and 4. The regressed migration fails, is rolled back, and the restored
    # baseline is verified independently.
    regressed = verify_candidate(REGRESSION_STAGE)
    regressed_decision = verifier.decide(impact, risk, regressed)
    steps.append(
        RecoveryDemoStep(
            stage=REGRESSION_STAGE,
            outcome=regressed.status,
            detail=(
                f"decision {regressed_decision.decision}; failing case(s): "
                + ", ".join(regressed.failure_evidence)
            ),
        )
    )

    with BenchmarkRollbackProvider() as provider:
        regressed_report = recovery.recover(
            impact, risk, regressed, regressed_decision, provider, cases
        )
        restored = provider.load_restored_baseline()
        restored_label = restored.resolve(PROBE_USER_ID)
        restored_report = CandidateVerifier().verify(
            restored, cases, migration_name=regressed.migration_name
        )
        steps.append(
            RecoveryDemoStep(
                stage=RESTORATION_STAGE,
                outcome=restored_report.status,
                detail=(
                    f"controlled restoration copied the known-good baseline into a "
                    f"temporary working copy; {PROBE_USER_ID} now returns "
                    f"{restored_label!r} (expected {PROBE_EXPECTED_LABEL!r})"
                ),
            )
        )

    steps.append(
        RecoveryDemoStep(
            stage=REGRESSION_STAGE,
            outcome=regressed_report.recovery_state,
            detail=(
                f"state path {' -> '.join(regressed_report.state_history)}; rollback "
                f"attempted={regressed_report.rollback_attempted}, succeeded="
                f"{regressed_report.rollback_succeeded}, baseline verification "
                f"{regressed_report.rollback_verification_status}"
            ),
        )
    )
    return tuple(steps)


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    """Print the demonstration. Returns a process exit code."""

    steps = run_recovery_demonstration()
    print("UPSHIFT controlled rollback demonstration")
    print("=" * 72)
    for step in steps:
        print(step.render())
    print("=" * 72)

    final = steps[-1]
    if final.outcome == COMPLETED:
        print(f"OK: recovery finished in {final.outcome}.")
        return 0
    print(f"UNEXPECTED: recovery finished in {final.outcome}.", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
