"""Deterministic evidence for the controlled rollback demonstration.

This module is part of the migration benchmark, not of the UPSHIFT engine. It
demonstrates the four benchmark stages the Phase 9 recovery layer supports:

1. the OLD baseline passes,
2. a correct migration is accepted,
3. an intentionally regressed migration fails independent verification,
4. the known-good baseline is restored and passes verification again, and
   recovery finishes in ``COMPLETED``.

Restoration happens in a temporary working copy, so the protected OLD baseline
implementation is never modified.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.core.recovery_engine import (
    ACCEPTED,
    COMPLETED,
    ROLLBACK_REQUIRED,
    ROLLING_BACK,
    ROLLBACK_VERIFICATION,
)
from app.verification.verifier import FAIL as VERIFICATION_FAIL, PASS as VERIFICATION_PASS
from demo.migration_benchmark.recovery_demo import (
    BASELINE_STAGE,
    CORRECT_STAGE,
    REGRESSION_STAGE,
    RESTORATION_STAGE,
    recover_candidate,
    run_recovery_demonstration,
)

BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_BASELINE = BENCHMARK_ROOT / "old" / "profile_service.py"
PROTECTED_BASELINE_DEPENDENCY = BENCHMARK_ROOT / "old" / "legacy_directory.py"
REGRESSION_SOURCE = (
    BENCHMARK_ROOT / "candidates" / REGRESSION_STAGE / "profile_service.py"
)
REGRESSION_DEMO = (
    BENCHMARK_ROOT
    / "candidates"
    / "regression_migration"
    / "tests"
    / "test_regression_demo.py"
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- stage 1: the OLD baseline -------------------------------------------


def test_old_baseline_passes_before_recovery():
    report = recover_candidate(BASELINE_STAGE)

    assert report.initial_verification_status == VERIFICATION_PASS
    assert report.entry_state == ACCEPTED
    assert report.recovery_state == COMPLETED
    assert report.rollback_attempted is False


def test_old_baseline_behavior_still_holds_directly():
    """The OLD service keeps its documented labels after recovery runs."""

    from demo.migration_benchmark.old.profile_service import ProfileLabelService

    service = ProfileLabelService()
    assert service.label_for("user-001") == "Ada Lovelace"
    assert service.label_for("user-002") == "Amazing Grace"
    assert service.label_for("user-003") == "Unknown"
    assert service.label_for("empty-user") == "Unnamed user"
    assert service.label_for("missing-user") == "Unknown user"


# -- stage 2: a correct migration -----------------------------------------


def test_correct_migration_is_accepted_without_recovery():
    report = recover_candidate(CORRECT_STAGE)

    assert report.initial_verification_status == VERIFICATION_PASS
    assert report.accepted is True
    assert report.state_history == (ACCEPTED, COMPLETED)
    assert report.rollback_attempted is False
    assert report.final_candidate_id == CORRECT_STAGE


# -- stage 3: the regressed migration fails before recovery ----------------


def test_regressed_migration_fails_independent_verification():
    report = recover_candidate(REGRESSION_STAGE)

    assert report.initial_verification_status == VERIFICATION_FAIL
    assert report.initial_decision == "REJECT"
    assert report.entry_state == ROLLBACK_REQUIRED


# -- stage 4: controlled rollback, verified baseline, COMPLETED -----------


def test_regressed_migration_rolls_back_and_completes():
    report = recover_candidate(REGRESSION_STAGE)

    assert report.state_history == (
        ROLLBACK_REQUIRED,
        ROLLING_BACK,
        ROLLBACK_VERIFICATION,
        COMPLETED,
    )
    assert report.rollback_attempted is True
    assert report.rollback_succeeded is True
    assert report.rollback_verification_status == VERIFICATION_PASS
    assert report.final_verification_status == VERIFICATION_PASS
    assert report.final_candidate_id == RESTORATION_STAGE
    # The migration itself is still rejected; only the baseline was restored.
    assert report.accepted is False


def test_rollback_unavailable_leaves_the_migration_inconclusive():
    report = recover_candidate(REGRESSION_STAGE, rollback_available=False)

    assert report.recovery_state == "INCONCLUSIVE"
    assert report.replanning_required is True
    assert report.replan_request is not None
    assert report.replan_request.status == "proposal"
    assert report.accepted is False


# -- determinism ----------------------------------------------------------


def test_demonstration_is_deterministic():
    first = run_recovery_demonstration()
    second = run_recovery_demonstration()

    assert first == second
    assert [step.stage for step in first] == [
        BASELINE_STAGE,
        CORRECT_STAGE,
        REGRESSION_STAGE,
        RESTORATION_STAGE,
        REGRESSION_STAGE,
    ]
    assert first[-1].outcome == COMPLETED


def test_demonstration_reports_the_probe_label_after_restoration():
    steps = run_recovery_demonstration()
    restoration = next(step for step in steps if step.stage == RESTORATION_STAGE)

    assert restoration.outcome == VERIFICATION_PASS
    assert "'Amazing Grace'" in restoration.detail


# -- the protected OLD baseline is never modified -------------------------


def test_rollback_does_not_modify_the_protected_old_baseline():
    before_service = _digest(PROTECTED_BASELINE)
    before_dependency = _digest(PROTECTED_BASELINE_DEPENDENCY)
    before_regression = _digest(REGRESSION_SOURCE)

    recover_candidate(REGRESSION_STAGE)

    assert _digest(PROTECTED_BASELINE) == before_service
    assert _digest(PROTECTED_BASELINE_DEPENDENCY) == before_dependency
    # The regression candidate source is untouched too, so the demonstration
    # still fails verification before recovery and cannot be silently "fixed".
    assert _digest(REGRESSION_SOURCE) == before_regression
    # The regression is still an actual behavioral regression.
    from demo.migration_benchmark.candidates.regression_migration.profile_service import (
        ProfileLabelService as RegressedService,
    )

    assert RegressedService().label_for("user-002") == "Grace Hopper"


def test_repeated_recovery_does_not_accumulate_temporary_state():
    import sys

    for _ in range(3):
        recover_candidate(REGRESSION_STAGE)

    leaked = [name for name in sys.modules if name.startswith("upshift_restored_baseline")]
    assert leaked == []


# -- the existing opt-in regression demonstration is not weakened ----------


def test_opt_in_regression_demonstration_remains_intact():
    source = REGRESSION_DEMO.read_text(encoding="utf-8")

    # The opt-in guard is still the only way to run the failing demonstration.
    assert "UPSHIFT_RUN_REGRESSION_DEMO" in source
    assert 'os.environ.get("UPSHIFT_RUN_REGRESSION_DEMO") != "1"' in source
    assert "Amazing Grace" in source
    assert re.search(r"assert\s+actual\s*==\s*\"Amazing Grace\"", source)
    # No skipif was weakened into an unconditional pass.
    assert "skipif" in source
    assert "xfail" not in source
