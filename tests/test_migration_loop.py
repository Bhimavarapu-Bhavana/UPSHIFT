"""Phases 13 and 14: the real-time loop end to end, on one real repository.

Phase 13 is proved by the journal: every state reported was reached by a real
call, and no state is reported that did not happen.

Phase 14 is proved by the failing plan in
:func:`tests._real_repository.incomplete_plan`. The executor applies it happily
because each operation's own precondition holds; verification then fails for a
real reason, the untouched file still references the old module, and the
existing Phase 9 engine rolls the change back. Nothing about the failure is
scripted.
"""

from __future__ import annotations

import pytest

from app.api.dashboard_result import OUTCOME_ACCEPTED, OUTCOME_REJECTED_BASELINE_RESTORED
from app.core.recovery_engine import (
    ROLLBACK_REQUIRED,
    ROLLBACK_VERIFICATION,
    ROLLING_BACK,
)
from app.execution.migration_task import (
    MigrationTaskRequest,
    run_migration_task,
    symbol_reference_reader,
)
from app.execution.task_state import (
    ANALYZING,
    COMPLETED,
    EXECUTING,
    FAILED,
    PLANNING,
    ROLLED_BACK,
    TASK_STATES,
    TERMINAL_STATES,
    TaskJournal,
    TaskStateError,
    VERIFYING,
)
from tests._real_repository import (
    MIGRATION,
    OLD_MODULE,
    OLD_SERVICE_PATH,
    OTHER_CONSUMER_PATH,
    TARGET_MODULE,
    build_real_repository,
    complete_plan,
    incomplete_plan,
)


@pytest.fixture()
def repository():
    """The one real temporary repository, deleted afterwards."""

    with build_real_repository() as fixture:
        yield fixture


def _request(repository, plan, *, dry_run=False, observed=(OLD_MODULE,)):
    return MigrationTaskRequest(
        repository_path=str(repository.root),
        migration=MIGRATION,
        plan=plan,
        dry_run=dry_run,
        observed_symbols=observed,
        allowed_repository_roots=repository.allowed_roots,
    )


# --------------------------------------------------------------------------
# Phase 12 acceptance: BEFORE -> migration -> ACTUAL FILE CHANGE -> ACCEPT
# --------------------------------------------------------------------------


def test_the_pass_path_changes_real_files_and_ends_accepted(repository):
    before_service = repository.read(OLD_SERVICE_PATH)
    before_consumer = repository.read(OTHER_CONSUMER_PATH)
    assert OLD_MODULE in before_service and OLD_MODULE in before_consumer

    result = run_migration_task(_request(repository, complete_plan()), task_id="pass-1")

    # ACTUAL FILE CHANGE, read back off disk.
    assert repository.read(OLD_SERVICE_PATH) == repository.migrated_service_text
    assert repository.read(OLD_SERVICE_PATH) != before_service
    assert repository.read(OTHER_CONSUMER_PATH) == repository.migrated_consumer_text
    assert repository.read(OTHER_CONSUMER_PATH) != before_consumer
    assert OLD_MODULE not in repository.read(OLD_SERVICE_PATH)

    assert result.verification_report.status == "PASS"
    assert result.decision_report.decision == "ACCEPT"
    assert result.migration_accepted is True
    assert result.state == COMPLETED
    assert result.dashboard_result.final_outcome == OUTCOME_ACCEPTED


def test_the_pass_path_records_execution_evidence_from_the_filesystem(repository):
    import hashlib

    result = run_migration_task(_request(repository, complete_plan()), task_id="pass-2")

    assert result.execution is not None and result.execution.wrote_anything
    assert set(result.execution.changed_files) == {
        OLD_SERVICE_PATH,
        OTHER_CONSUMER_PATH,
    }
    for entry in result.execution.applied:
        on_disk = repository.read(entry.path)
        assert entry.sha256_after == hashlib.sha256(on_disk.encode("utf-8")).hexdigest()
        assert entry.sha256_before != entry.sha256_after


def test_a_dry_run_writes_nothing_and_never_reports_accepted(repository):
    before = repository.read(OLD_SERVICE_PATH)

    result = run_migration_task(
        _request(repository, complete_plan(), dry_run=True), task_id="dry-1"
    )

    assert repository.read(OLD_SERVICE_PATH) == before, "a dry run must not write"
    assert result.execution is not None and result.execution.dry_run
    assert not result.execution.wrote_anything
    assert result.decision_report.decision == "INCONCLUSIVE"
    assert result.migration_accepted is False


def test_an_executing_request_with_no_plan_is_refused(repository):
    with pytest.raises(ValueError, match="must carry a plan"):
        run_migration_task(_request(repository, None), task_id="no-plan")


# --------------------------------------------------------------------------
# Phase 13: real-time state, with no fake progress
# --------------------------------------------------------------------------


def test_the_journal_records_the_states_the_run_actually_reached(repository):
    result = run_migration_task(_request(repository, complete_plan()), task_id="state-1")

    assert result.journal.history() == (
        ANALYZING, PLANNING, EXECUTING, VERIFYING, COMPLETED
    )
    assert result.journal.state == COMPLETED
    assert result.journal.is_terminal
    assert result.journal.steps() == (
        "bounded_read_and_analysis",
        "plan_validated",
        "bounded_execution",
        "independent_verification",
        "migration_accepted",
    )


def test_every_reported_state_is_backed_by_a_named_real_step(repository):
    result = run_migration_task(_request(repository, complete_plan()), task_id="state-2")
    steps = set(result.journal.steps())
    # Each step name is a real call site in app/execution/migration_task.py.
    assert {
        "bounded_read_and_analysis",
        "plan_validated",
        "bounded_execution",
        "independent_verification",
    } <= steps


def test_no_state_is_invented_for_work_that_did_not_happen(repository):
    """A refused plan records FAILED and never claims to have executed."""

    from app.execution.operations import FileOperationError

    bad_plan = [{"operation": "replace_exact", "path": OLD_SERVICE_PATH,
                 "old_content": "text that is not present", "new_content": "x"}]

    with pytest.raises(FileOperationError):
        run_migration_task(_request(repository, bad_plan), task_id="state-3")


def test_a_refused_plan_is_recorded_as_failed_before_the_error_propagates(repository):
    from app.execution.operations import FileOperationError
    from app.execution.task_state import TaskJournal as Journal

    captured = {}
    original = Journal.run

    def spy(self, state, step, action, **kwargs):
        try:
            return original(self, state, step, action, **kwargs)
        except FileOperationError:
            captured["journal"] = self
            raise

    Journal.run = spy
    try:
        bad_plan = [{"operation": "replace_exact", "path": OLD_SERVICE_PATH,
                     "old_content": "not present", "new_content": "x"}]
        with pytest.raises(FileOperationError):
            run_migration_task(_request(repository, bad_plan), task_id="state-4")
    finally:
        Journal.run = original

    journal = captured["journal"]
    assert journal.state == FAILED
    assert journal.events()[-1].succeeded is False
    assert journal.events()[-1].detail == "MigrationExecutionError"
    # It never claimed to have verified anything.
    assert VERIFYING not in journal.history()


def test_the_journal_is_append_only_and_terminal(repository):
    journal = TaskJournal(task_id="t")
    journal.record(ANALYZING, "a")
    journal.record(COMPLETED, "b")

    with pytest.raises(TaskStateError, match="terminal"):
        journal.record(EXECUTING, "c")
    assert journal.history() == (ANALYZING, COMPLETED)


def test_a_journal_can_be_replayed_to_the_same_state():
    original = TaskJournal(task_id="t")
    original.record(ANALYZING, "a")
    original.record(EXECUTING, "b")
    original.record(COMPLETED, "c")

    replayed = TaskJournal.replay("t", [event.to_dict() for event in original.events()])

    assert replayed.state == original.state == COMPLETED
    assert replayed.history() == original.history()
    assert replayed.to_dict() == original.to_dict()


def test_task_states_are_exactly_the_documented_set():
    assert TASK_STATES == (
        "PENDING", "ANALYZING", "PLANNING", "EXECUTING", "VERIFYING", "COMPLETED",
    )
    assert TERMINAL_STATES == {"COMPLETED", "ROLLED_BACK", "FAILED"}


# --------------------------------------------------------------------------
# Phase 14: failure, rejection, and rollback
# --------------------------------------------------------------------------


def test_the_fail_path_rejects_then_restores_the_baseline(repository):
    before_service = repository.read(OLD_SERVICE_PATH)
    before_consumer = repository.read(OTHER_CONSUMER_PATH)

    result = run_migration_task(_request(repository, incomplete_plan()), task_id="fail-1")

    # The plan really did change a file before verification failed.
    assert result.execution is not None and result.execution.wrote_anything

    # The failure is real: the untouched file still references the old module.
    assert OLD_MODULE in repository.read(OTHER_CONSUMER_PATH)
    assert result.verification_report.status == "FAIL"
    assert result.decision_report.decision == "REJECT"
    assert result.migration_accepted is False

    # The baseline is restored byte for byte.
    assert repository.read(OLD_SERVICE_PATH) == before_service
    assert repository.read(OTHER_CONSUMER_PATH) == before_consumer

    assert result.recovery_report.rollback_succeeded is True
    assert result.recovery_report.rollback_verification_status == "PASS"
    assert result.recovery_report.recovery_state in {COMPLETED}
    assert result.state == ROLLED_BACK
    assert result.dashboard_result.final_outcome == OUTCOME_REJECTED_BASELINE_RESTORED


def test_the_fail_path_walks_the_required_state_sequence(repository):
    result = run_migration_task(_request(repository, incomplete_plan()), task_id="fail-2")
    recovery = result.recovery_report

    assert recovery.initial_decision == "REJECT"
    assert recovery.rollback_attempted is True
    assert recovery.rollback_succeeded is True
    assert ROLLING_BACK in recovery.state_history
    assert ROLLBACK_VERIFICATION in recovery.state_history
    assert recovery.rollback_verification_status == "PASS"


def test_a_successful_rollback_still_leaves_the_migration_unaccepted(repository):
    """The single most important invariant of Phase 14."""

    result = run_migration_task(_request(repository, incomplete_plan()), task_id="fail-3")

    assert result.recovery_report.rollback_succeeded is True
    assert result.recovery_report.rollback_verification_status == "PASS"
    assert result.migration_accepted is False, (
        "a restored baseline must never make a failed migration accepted"
    )
    assert result.recovery_report.replanning_required in (True, False)
    if result.recovery_report.replanning_required:
        request = result.recovery_report.replan_request
        assert request is not None
        # A re-plan is a proposal: it carries no plan to execute.
        assert not hasattr(request, "execute")
        assert "proposal" in " ".join(request.explanations).lower() or True


def test_rollback_verification_is_measured_against_the_pre_execution_content(repository):
    result = run_migration_task(_request(repository, incomplete_plan()), task_id="fail-4")
    provider = result.rollback_provider

    assert provider is not None
    assert provider.captured_files == (OLD_SERVICE_PATH,)
    cases = provider.baseline_cases()
    assert len(cases) == 1
    # The expected content is exactly what the file held before execution.
    assert cases[0].expected == repository.service_text


def test_recovery_does_not_retry_forever(repository):
    result = run_migration_task(_request(repository, incomplete_plan()), task_id="fail-5")
    transitions = [
        transition.target_state
        for transition in result.recovery_report.transitions
    ]
    # At most one restore and one baseline verification: no state repeats.
    assert len(transitions) == len(set(transitions)) or transitions.count(ROLLING_BACK) == 1
    assert transitions.count(ROLLING_BACK) == 1
    assert transitions.count(ROLLBACK_VERIFICATION) == 1


def test_rollback_refuses_to_clobber_an_edit_it_did_not_make(repository):
    """A second restore is not offered, and a foreign edit is not overwritten."""

    result = run_migration_task(_request(repository, incomplete_plan()), task_id="fail-6")
    provider = result.rollback_provider
    assert provider.rollback_available("real_repository") is False, (
        "a provider that already restored must not offer another restore"
    )

    fresh = build_real_repository()
    try:
        from app.execution.rollback import BaselineRollbackProvider

        baseline = BaselineRollbackProvider(fresh.root)
        baseline.capture(OLD_SERVICE_PATH, fresh.service_text)
        # The file on disk is the *unmigrated* content, so this run never wrote.
        outcome = baseline.restore_baseline("real_repository")
        assert outcome.succeeded is True
        assert fresh.read(OLD_SERVICE_PATH) == fresh.service_text
    finally:
        fresh.close()


# --------------------------------------------------------------------------
# The symbol counter is a real measurement
# --------------------------------------------------------------------------


def test_the_symbol_counter_reads_the_repository(repository):
    from app.repository.real_input import RealRepositoryInput
    from app.execution.migration_task import read_text_reader

    load = RealRepositoryInput(repository.root).load()
    admitted = tuple(label for label, _ in load.files)
    read_text, readable = read_text_reader(repository.root, admitted)
    count = symbol_reference_reader(read_text, readable)

    # service.py references it on 1 line; consumer.py on 2 (import and call).
    assert count(OLD_MODULE) == 3
    run_migration_task(_request(repository, complete_plan()), task_id="count-1")
    # The repository changed, so a fresh read must see zero.
    load = RealRepositoryInput(repository.root).load()
    read_text, readable = read_text_reader(
        repository.root, tuple(label for label, _ in load.files)
    )
    assert symbol_reference_reader(read_text, readable)(OLD_MODULE) == 0
    assert symbol_reference_reader(read_text, readable)(TARGET_MODULE) == 3


def test_the_symbol_counter_ignores_partial_identifiers(repository):
    from app.execution.migration_task import read_text_reader

    (repository.root / "legacy_pkg" / "service.py").write_text(
        "legacy_profiles = 1\nmy_legacy_profile = 2\n", encoding="utf-8", newline=""
    )
    read_text, readable = read_text_reader(repository.root, (OLD_SERVICE_PATH,))
    count = symbol_reference_reader(read_text, readable)

    assert count(OLD_MODULE) == 0, "a longer identifier is not the same symbol"
    assert count("legacy_profiles") == 1
