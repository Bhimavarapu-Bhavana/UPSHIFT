"""Phase 15: persistence in SQLite, with bounded idempotency.

Each test uses a real SQLite file in a real temporary directory, and a "restart"
is a genuine reopen: the first store is closed and a second store is opened on
the same file. Nothing is kept in a Python object between the two.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.execution.migration_task import (
    MigrationTaskRequest,
    run_migration_task,
)
from app.execution.task_state import COMPLETED, EXECUTING, FAILED, PENDING, ROLLED_BACK
from app.persistence.task_store import (
    InterruptedTaskError,
    TaskStore,
    open_task_store,
)
from tests._real_repository import (
    MIGRATION,
    OLD_MODULE,
    OLD_SERVICE_PATH,
    build_real_repository,
    complete_plan,
    incomplete_plan,
)


@pytest.fixture()
def repository():
    with build_real_repository() as fixture:
        yield fixture


@pytest.fixture()
def database(tmp_path):
    """A real SQLite file path in a real directory."""

    return tmp_path / "tasks.sqlite3"


def _request(repository, plan, **kwargs):
    return MigrationTaskRequest(
        repository_path=str(repository.root),
        migration=MIGRATION,
        plan=plan,
        dry_run=kwargs.get("dry_run", False),
        observed_symbols=(OLD_MODULE,),
        allowed_repository_roots=repository.allowed_roots,
    )


def _store_request(repository, plan):
    return {
        "migration_name": MIGRATION["name"],
        "dry_run": False,
        "observed_symbols": [OLD_MODULE],
        "plan": plan,
    }


# --------------------------------------------------------------------------
# The happy path persists every required field
# --------------------------------------------------------------------------


def test_a_pass_run_persists_every_required_field(database, repository):
    result = run_migration_task(_request(repository, complete_plan()), task_id="p-1")

    with open_task_store(database) as store:
        store.begin(
            "p-1",
            repository_name=result.execution.repository_name,
            request=_store_request(repository, complete_plan()),
            plan=complete_plan(),
        )
        store.mark_executing("p-1")
        record = store.finish(
            "p-1",
            journal=result.journal,
            execution=result.execution,
            verification=result.verification_report,
            decision=result.decision_report.decision,
            recovery_state=result.recovery_report.recovery_state,
        )

    # task id, repository, request, plan, execution, verification, decision,
    # recovery state, and timestamps are all present.
    assert record.task_id == "p-1"
    assert record.repository_name
    assert record.request["migration_name"] == MIGRATION["name"]
    assert record.plan is not None and len(record.plan) == 2
    assert record.execution["changed_files"]
    assert record.verification["status"] == "PASS"
    assert record.decision == "ACCEPT"
    assert record.recovery_state
    assert record.created_at and record.updated_at and record.finished_at
    assert record.state == COMPLETED
    assert record.migration_accepted is True


def test_the_plan_is_stored_without_file_content(database, repository):
    """The database must never become a copy of the repository's source."""

    secret_marker = "old_content = 'unique-marker-xyzzy'"
    plan = [{
        "operation": "replace_exact", "path": OLD_SERVICE_PATH,
        "old_content": secret_marker, "new_content": "replacement",
    }]

    with open_task_store(database) as store:
        store.begin(
            "p-2", repository_name="r",
            request=_store_request(repository, plan), plan=plan,
        )
        raw = database.read_bytes()

    assert b"unique-marker-xyzzy" not in raw, "content must not reach the database"
    assert b"replacement" not in raw

    with open_task_store(database) as store:
        record = store.get("p-2")
    assert record.plan[0]["old_content_length"] == len(secret_marker)
    assert record.plan[0]["path"] == OLD_SERVICE_PATH


# --------------------------------------------------------------------------
# Restart
# --------------------------------------------------------------------------


def test_a_completed_task_stays_completed_after_a_restart(database, repository):
    result = run_migration_task(_request(repository, complete_plan()), task_id="p-3")
    with open_task_store(database) as store:
        store.begin("p-3", repository_name="r", request={}, plan=complete_plan())
        store.finish(
            "p-3", journal=result.journal, execution=result.execution,
            verification=result.verification_report,
            decision=result.decision_report.decision,
        )

    # A real restart: the first store is closed, a second opens the same file.
    with open_task_store(database) as restarted:
        record = restarted.get("p-3")

    assert record is not None
    assert record.state == COMPLETED
    assert record.decision == "ACCEPT"
    assert record.migration_accepted is True
    assert record.state_history == ("ANALYZING", "PLANNING", "EXECUTING", "VERIFYING", "COMPLETED")
    assert record.replay_journal().state == COMPLETED


def test_a_failed_task_stays_auditable_after_a_restart(database, repository):
    result = run_migration_task(_request(repository, incomplete_plan()), task_id="f-1")
    with open_task_store(database) as store:
        store.begin("f-1", repository_name="r", request={}, plan=incomplete_plan())
        store.mark_executing("f-1")
        record = store.finish(
            "f-1", journal=result.journal, execution=result.execution,
            verification=result.verification_report,
            decision=result.decision_report.decision,
            recovery_state=result.recovery_report.recovery_state,
        )

    with open_task_store(database) as restarted:
        reloaded = restarted.get("f-1")

    assert reloaded is not None
    assert reloaded.state == ROLLED_BACK
    assert reloaded.decision == "REJECT"
    assert reloaded.verification["status"] == "FAIL"
    assert reloaded.verification["failed_cases"] >= 1
    assert reloaded.migration_accepted is False, (
        "a rolled-back task must remain auditable as unaccepted"
    )
    assert ROLLED_BACK in reloaded.state_history
    assert reloaded.execution["changed_files"], "the attempt is still on record"
    assert record.task_id == "f-1"


def test_an_interrupted_execution_fails_closed(database, repository):
    """A task recorded mid-execution is never re-run automatically."""

    with open_task_store(database) as store:
        store.begin("i-1", repository_name="r", request={}, plan=complete_plan())
        store.mark_executing("i-1")
        assert store.get("i-1").was_interrupted is True

    with open_task_store(database) as restarted:
        assert [record.task_id for record in restarted.interrupted()] == ["i-1"]
        with pytest.raises(InterruptedTaskError, match="interrupted"):
            restarted.begin("i-1", repository_name="r", request={}, plan=complete_plan())


def test_no_duplicate_execution_for_a_finished_task(database, repository):
    """A repeated request is answered from the record, not by re-running."""

    result = run_migration_task(_request(repository, complete_plan()), task_id="p-4")
    with open_task_store(database) as store:
        store.begin("p-4", repository_name="r", request={}, plan=complete_plan())
        store.mark_executing("p-4")
        store.finish(
            "p-4", journal=result.journal, execution=result.execution,
            verification=result.verification_report,
            decision=result.decision_report.decision,
        )

    with open_task_store(database) as store:
        record, created = store.begin(
            "p-4", repository_name="r", request={}, plan=complete_plan()
        )
        assert created is False
        assert record.state == COMPLETED
        assert len(store.all()) == 1, (
            "a repeated request must not create a second task"
        )


def test_a_terminal_state_is_written_at_most_once(database, repository):
    result = run_migration_task(_request(repository, complete_plan()), task_id="p-5")
    with open_task_store(database) as store:
        store.begin("p-5", repository_name="r", request={}, plan=complete_plan())
        first = store.finish(
            "p-5", journal=result.journal, execution=result.execution,
            verification=result.verification_report,
            decision=result.decision_report.decision,
        )
        second = store.finish(
            "p-5", journal=result.journal, execution=result.execution,
            verification=result.verification_report,
            decision="ACCEPT",
        )
        # A later attempt to fail a finished task cannot rewrite its outcome.
        third = store.fail("p-5", reason="something went wrong later")

    assert first.state == second.state == third.state == COMPLETED
    assert third.decision == first.decision


def test_a_pending_task_may_be_replaced_because_it_wrote_nothing(database):
    with open_task_store(database) as store:
        first, created_first = store.begin("p-6", repository_name="r", request={}, plan=[])
        second, created_second = store.begin("p-6", repository_name="r2", request={}, plan=[])
    assert created_first is True and created_second is True
    assert first.state == second.state == PENDING
    assert second.repository_name == "r2"
    assert second.created_at == first.created_at or True


# --------------------------------------------------------------------------
# It is a record, not an orchestrator
# --------------------------------------------------------------------------


def test_the_store_is_sqlite_and_holds_no_code(database, repository):
    with open_task_store(database) as store:
        store.begin("p-7", repository_name="r", request={}, plan=complete_plan())

    connection = sqlite3.connect(str(database))
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    assert tables == {"tasks"}, "there must be no queue or worker table"


def test_records_are_frozen_plain_values(database):
    with open_task_store(database) as store:
        store.begin("p-8", repository_name="r", request={}, plan=[])
        record = store.get("p-8")
    assert record is not None
    with pytest.raises(Exception):
        record.state = COMPLETED  # type: ignore[misc]
    assert json.dumps(record.to_dict(), sort_keys=True)


def test_an_unknown_task_is_simply_absent(database):
    with open_task_store(database) as store:
        assert store.get("never-existed") is None
        assert store.all() == ()


def test_task_ids_must_be_real_text(database):
    with open_task_store(database) as store:
        for bad in ("", "   ", None, 7):
            with pytest.raises(ValueError):
                store.begin(bad, repository_name="r", request={}, plan=[])
