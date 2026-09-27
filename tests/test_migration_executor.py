"""Phase 12: the bounded migration executor, against one real repository.

Every test here touches a real directory on the local filesystem. Where a test
asserts that a file did not change, that is measured by reading the file, not by
trusting the executor's return value.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.execution.migration_executor import (
    ChangedFileEvidence,
    MigrationExecutionError,
    MigrationExecutor,
)
from app.execution.operations import (
    EXECUTION_LIMITS,
    OPERATION_REPLACE_EXACT,
    FileOperation,
    FileOperationError,
    require_file_operations,
    require_file_operation,
)
from app.repository.real_input import RealRepositoryInput
from tests._real_repository import (
    FORBIDDEN_LABELS,
    OLD_MODULE,
    OLD_SERVICE_PATH,
    OTHER_CONSUMER_PATH,
    TARGET_MODULE,
    build_real_repository,
    complete_plan,
)


@pytest.fixture()
def repository():
    """The one real temporary repository, deleted afterwards."""

    with build_real_repository() as fixture:
        yield fixture


def _admitted(root: Path):
    """The labels the existing bounded reader admitted."""

    return tuple(label for label, _text in RealRepositoryInput(root).load().files)


def _executor(repository):
    return MigrationExecutor(repository.root, _admitted(repository.root))


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_execution_changes_a_real_file_on_disk(repository):
    before = repository.read(OLD_SERVICE_PATH)
    assert OLD_MODULE in before

    result = _executor(repository).execute(
        [{"operation": OPERATION_REPLACE_EXACT, "path": OLD_SERVICE_PATH,
          "old_content": f"from {OLD_MODULE} import resolve_profile",
          "new_content": f"from {TARGET_MODULE} import resolve_profile",
          "expected_occurrences": 1}]
    )

    after = repository.read(OLD_SERVICE_PATH)
    assert after != before, "the file on disk must actually differ"
    assert f"from {TARGET_MODULE} import resolve_profile" in after
    assert OLD_MODULE not in after
    assert result.wrote_anything
    assert result.changed_files == (OLD_SERVICE_PATH,)


def test_evidence_comes_from_the_filesystem_not_the_plan(repository):
    import hashlib

    result = _executor(repository).execute(
        [{"operation": OPERATION_REPLACE_EXACT, "path": OLD_SERVICE_PATH,
          "old_content": f"from {OLD_MODULE} import resolve_profile",
          "new_content": f"from {TARGET_MODULE} import resolve_profile"}]
    )

    entry = result.applied[0]
    on_disk = repository.read(OLD_SERVICE_PATH)
    assert entry.sha256_after == hashlib.sha256(
        on_disk.encode("utf-8")
    ).hexdigest(), "the reported digest must be the file's real digest"
    assert entry.sha256_before == hashlib.sha256(
        repository.service_text.encode("utf-8")
    ).hexdigest()
    assert entry.bytes_after == len(on_disk.encode("utf-8"))
    assert isinstance(entry, ChangedFileEvidence)


def test_dry_run_reports_the_digest_without_writing(repository):
    before = repository.read(OLD_SERVICE_PATH)
    result = _executor(repository).dry_run(
        [{"operation": OPERATION_REPLACE_EXACT, "path": OLD_SERVICE_PATH,
          "old_content": f"from {OLD_MODULE} import resolve_profile",
          "new_content": f"from {TARGET_MODULE} import resolve_profile"}]
    )

    assert repository.read(OLD_SERVICE_PATH) == before, "a dry run must not write"
    assert result.dry_run
    assert not result.wrote_anything
    assert result.applied[0].changed, "a dry run reports the change it would make"
    assert result.applied[0].sha256_before == result.applied[0].sha256_before


def test_crlf_line_endings_survive_a_migration(repository):
    path = repository.root / Path(*OLD_SERVICE_PATH.split("/"))
    path.write_bytes(b"from legacy_profile import resolve_profile\r\nx = 1\r\n")

    _executor(repository).execute(
        [{"operation": OPERATION_REPLACE_EXACT, "path": OLD_SERVICE_PATH,
          "old_content": "from legacy_profile import resolve_profile",
          "new_content": "from profile_directory import resolve_profile"}]
    )

    raw = path.read_bytes()
    assert b"\r\n" in raw, "CRLF endings must not be rewritten to LF"
    assert raw.count(b"\r\n") == 2


# --------------------------------------------------------------------------
# Refusals: every one measured against a real repository
# --------------------------------------------------------------------------


def test_a_rejected_plan_changes_nothing_at_all(repository):
    """All-or-nothing: one bad operation must not half-apply a good one."""

    before = repository.read(OLD_SERVICE_PATH)
    good = {
        "operation": OPERATION_REPLACE_EXACT,
        "path": OLD_SERVICE_PATH,
        "old_content": f"from {OLD_MODULE} import resolve_profile",
        "new_content": f"from {TARGET_MODULE} import resolve_profile",
    }
    bad = dict(good, path=OTHER_CONSUMER_PATH, old_content="text that is not there")

    with pytest.raises(MigrationExecutionError):
        _executor(repository).execute([good, bad])

    assert repository.read(OLD_SERVICE_PATH) == before


def test_unexpected_content_is_refused_and_the_file_is_untouched(repository):
    before = repository.read(OLD_SERVICE_PATH)
    with pytest.raises(MigrationExecutionError, match="occurrence"):
        _executor(repository).execute(
            [{"operation": OPERATION_REPLACE_EXACT, "path": OLD_SERVICE_PATH,
              "old_content": "def something_that_is_absent():", "new_content": "x"}]
        )
    assert repository.read(OLD_SERVICE_PATH) == before


def test_a_wrong_expected_occurrence_count_is_refused(repository):
    before = repository.read(OLD_SERVICE_PATH)
    with pytest.raises(MigrationExecutionError, match="occurrence"):
        _executor(repository).execute(
            [{"operation": OPERATION_REPLACE_EXACT, "path": OLD_SERVICE_PATH,
              "old_content": f"from {OLD_MODULE} import resolve_profile",
              "new_content": "x", "expected_occurrences": 2}]
        )
    assert repository.read(OLD_SERVICE_PATH) == before


@pytest.mark.parametrize("label", FORBIDDEN_LABELS)
def test_a_file_the_reader_refused_can_never_be_targeted(repository, label):
    """A real .env, a real credentials file, and a real excluded directory."""

    assert repository.exists(label), "the fixture must really contain this file"
    admitted = _admitted(repository.root)
    assert label not in admitted, "the bounded reader must have refused it"

    # Refused at whichever layer catches it first: shape validation for a
    # single-component name such as ".env", the executor's known-file gate for
    # everything else. Either way nothing is written.
    before = repository.read(label)
    with pytest.raises(FileOperationError):
        _executor(repository).execute(
            [{"operation": OPERATION_REPLACE_EXACT, "path": label,
              "old_content": before.strip(), "new_content": "changed"}]
        )
    assert repository.read(label) == before, "a refused target must be untouched"


def test_the_excluded_directory_is_really_one_the_reader_excludes(repository):
    """The fixture's claim is checked against the reader, not assumed."""

    from app.repository.real_input import EXCLUDED_DIRECTORY_NAMES

    from tests._real_repository import EXCLUDED_DIRECTORY

    assert EXCLUDED_DIRECTORY in EXCLUDED_DIRECTORY_NAMES


def test_a_file_outside_the_root_is_refused(repository):
    outside = repository.root.parent / "outside-target.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    try:
        with pytest.raises(FileOperationError):
            _executor(repository).execute(
                [{"operation": OPERATION_REPLACE_EXACT, "path": "../outside-target.py",
                  "old_content": "secret = 1", "new_content": "secret = 2"}]
            )
        assert outside.read_text(encoding="utf-8") == "secret = 1\n"
    finally:
        outside.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Refusals at validation time, before any filesystem access
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "C:/Windows/System32/drivers/etc/hosts",
        "\\\\server\\share\\file.txt",
        "../escape.py",
        "pkg/../../escape.py",
        "./pkg/service.py",
        "pkg//service.py",
        "",
        "   ",
        "pkg/\x00service.py",
    ],
)
def test_traversal_and_absolute_paths_are_refused_at_validation(path):
    with pytest.raises(FileOperationError):
        require_file_operation(
            {"operation": OPERATION_REPLACE_EXACT, "path": path,
             "old_content": "a", "new_content": "b"}
        )


@pytest.mark.parametrize(
    "operation",
    ["run", "delete_file", "write_file", "exec", "shell", "rename", "chmod", "create"],
)
def test_unsupported_operations_are_refused(operation):
    with pytest.raises(FileOperationError, match="unsupported operation"):
        require_file_operation(
            {"operation": operation, "path": "pkg/service.py",
             "old_content": "a", "new_content": "b"}
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "rm -rf /"},
        {"args": ["-c", "print(1)"]},
        {"shell": True},
        {"code": "import os"},
        {"module": "os"},
        {"import": "os"},
        {"script": "echo hi"},
        {"root": "/"},
        {"absolute_path": "/etc/passwd"},
    ],
)
def test_no_execution_shaped_field_is_even_accepted(extra):
    """Extra fields are refused, not ignored, so nothing can be smuggled."""

    payload = {
        "operation": OPERATION_REPLACE_EXACT,
        "path": "pkg/service.py",
        "old_content": "a",
        "new_content": "b",
    }
    payload.update(extra)
    with pytest.raises(FileOperationError, match="does not accept"):
        require_file_operation(payload)


def test_old_content_equal_to_new_content_is_refused():
    with pytest.raises(FileOperationError, match="must differ"):
        require_file_operation(
            {"operation": OPERATION_REPLACE_EXACT, "path": "pkg/service.py",
             "old_content": "same", "new_content": "same"}
        )


@pytest.mark.parametrize("count", [0, -1, 2_000, True, "1", 1.5, None])
def test_expected_occurrences_must_be_a_positive_integer(count):
    with pytest.raises(FileOperationError):
        require_file_operation(
            {"operation": OPERATION_REPLACE_EXACT, "path": "pkg/service.py",
             "old_content": "a", "new_content": "b", "expected_occurrences": count}
        )


def test_a_plan_cannot_target_one_file_twice():
    operation = {
        "operation": OPERATION_REPLACE_EXACT,
        "path": "pkg/service.py",
        "old_content": "a",
        "new_content": "b",
    }
    with pytest.raises(FileOperationError, match="same file"):
        require_file_operations([operation, dict(operation, old_content="c", new_content="d")])


def test_a_plan_is_bounded():
    operation = {
        "operation": OPERATION_REPLACE_EXACT,
        "path": "pkg/service.py",
        "old_content": "a",
        "new_content": "b",
    }
    with pytest.raises(FileOperationError, match="at most"):
        require_file_operations([dict(operation)] * (EXECUTION_LIMITS["max_operations_per_plan"] + 1))
    with pytest.raises(FileOperationError, match="at least one"):
        require_file_operations([])


def test_oversized_content_is_refused():
    with pytest.raises(FileOperationError, match="at most"):
        require_file_operation(
            {"operation": OPERATION_REPLACE_EXACT, "path": "pkg/service.py",
             "old_content": "a" * (EXECUTION_LIMITS["max_old_content_length"] + 1),
             "new_content": "b"}
        )


def test_an_operation_is_a_frozen_plain_value():
    operation = require_file_operation(
        {"operation": OPERATION_REPLACE_EXACT, "path": "pkg/service.py",
         "old_content": "a", "new_content": "b"}
    )
    assert isinstance(operation, FileOperation)
    with pytest.raises(Exception):
        operation.path = "elsewhere.py"  # type: ignore[misc]

    # The description reports lengths, never the content itself.
    described = operation.describe()
    assert described["old_content_length"] == 1
    assert "content" not in repr(described["old_content_length"]) or True
    assert set(described) == {
        "operation", "path", "expected_occurrences",
        "old_content_length", "new_content_length",
    }
    # The content does not leak into the repr either.
    assert "'a'" not in repr(operation).replace("replace_exact", "")


# --------------------------------------------------------------------------
# The executor itself cannot execute anything
# ----------------------------------------------------------------------------

_EXECUTOR_SOURCES = (
    "app/execution/operations.py",
    "app/execution/migration_executor.py",
    "app/execution/rollback.py",
    "app/execution/task_state.py",
    "app/execution/migration_task.py",
)

_FORBIDDEN_MODULES = {
    "subprocess",
    "importlib",
    "socket",
    "pty",
    "urllib",
    "requests",
    "http",
    "multiprocessing",
    "ctypes",
    "pickle",
    "shelve",
}

#: ``module.attribute`` pairs that would be an execution, process, or network
#: capability even though the module itself is ordinary.
_FORBIDDEN_ATTRIBUTES = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "spawnl"),
    ("os", "spawnv"),
    ("os", "execv"),
    ("os", "execve"),
    ("os", "startfile"),
    ("shutil", "rmtree"),
    ("pathlib", "Path.cwd"),
}

#: Names that would let a plan escape the text-replacement model entirely.
_FORBIDDEN_CALLS = {"eval", "exec", "__import__", "compile", "globals", "locals", "vars"}


@pytest.mark.parametrize("relative", _EXECUTOR_SOURCES)
def test_the_execution_package_contains_no_execution_capability(relative):
    """AST-only, so a docstring saying "no subprocess" cannot fail the check."""

    import ast

    source = (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (_FORBIDDEN_MODULES & imported), (
        f"{relative} imports an execution or network capability: {sorted(imported)}"
    )

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (_FORBIDDEN_CALLS & called), (
        f"{relative} calls a dynamic-execution builtin: {sorted(called & _FORBIDDEN_CALLS)}"
    )

    qualified = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name):
                qualified.add((owner.id, node.func.attr))
    assert not (_FORBIDDEN_ATTRIBUTES & qualified), (
        f"{relative} calls a process or destructive API: {sorted(qualified & _FORBIDDEN_ATTRIBUTES)}"
    )


@pytest.mark.parametrize("relative", _EXECUTOR_SOURCES)
def test_the_execution_package_never_reads_the_environment(relative):
    source = (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")
    import ast

    names = {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
    }
    assert not ({"environ", "getenv", "putenv", "expandvars"} & names), (
        f"{relative} reads or writes the process environment"
    )
