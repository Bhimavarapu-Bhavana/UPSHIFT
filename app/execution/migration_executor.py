"""The bounded executor: the only code in UPSHIFT that writes to a repository.

Every write passes four gates, in this order, and a failure at any gate stops
the plan before a single byte is written:

1. **Shape.** :func:`app.execution.operations.require_file_operations` refuses
   an unsupported operation, an absolute or traversing path, extra fields, and
   duplicate targets.
2. **Known file.** The target must be one of the labels the existing bounded
   reader admitted. That single rule excludes files outside the boundary, links
   and junctions, excluded directories, sensitive names, unreadable files, and
   anything beyond the read budgets, because those never reach ``load.files``.
3. **Containment.** The joined path is resolved and re-checked against the
   approved root. This is independent of gate 2 and of the reader, so a plan
   cannot rely on one check being correct.
4. **Precondition.** The file's current content must contain ``old_content``
   exactly ``expected_occurrences`` times. A mismatch is refused, not guessed
   at. This is what makes the change safe to review in advance: the plan states
   what it expects, and the filesystem has to agree.

The write itself is textual and atomic. Newline handling is verbatim in both
directions (``newline=""``), so a CRLF file is not silently rewritten to LF.
The new text is written to a sibling temporary file and moved into place with
:func:`os.replace`, so a crash mid-write cannot leave a half-written source
file.

Deliberately absent
-------------------
No shell, no subprocess, no interpreter, no ``eval``/``exec``/``__import__``, no
network, no Git, no environment read, and no credential access. This module
imports nothing that can start a process. Its entire vocabulary is
``open``/``read``/``write``/``replace`` on paths the boundary already approved.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from app.execution.operations import (
    FileOperation,
    FileOperationError,
    require_file_operations,
)

__all__ = [
    "ChangedFileEvidence",
    "ExecutionResult",
    "MigrationExecutionError",
    "MigrationExecutor",
    "RESTORE_MECHANISM",
]

#: Reported as the restoration mechanism for a real-repository run. The existing
#: recovery engine only ever reports a fact here, so this is a description of
#: what the executor can do, not a promise that it ran.
RESTORE_MECHANISM = "pre_execution_content_capture"

_BACKUP_CALLBACK = Callable[[str, str], None]


class MigrationExecutionError(FileOperationError):
    """Raised when a plan cannot be applied to the repository as it stands.

    Carries the same non-echoing messages as :class:`FileOperationError`: the
    reason and the repository-relative target, never the content.
    """


@dataclass(frozen=True)
class ChangedFileEvidence:
    """What actually happened to one file, measured after the fact.

    Attributes:
        path: Repository-relative path.
        occurrences_expected: The count the plan required.
        occurrences_found: The count actually present before the write.
        bytes_before: File size before the write.
        bytes_after: File size after the write.
        sha256_before: Content digest before the write.
        sha256_after: Content digest after the write.
        changed: False only for a dry run, or for a no-op replacement.
    """

    path: str
    occurrences_expected: int
    occurrences_found: int
    bytes_before: int
    bytes_after: int
    sha256_before: str
    sha256_after: str
    changed: bool

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable projection."""

        return {
            "path": self.path,
            "occurrences_expected": self.occurrences_expected,
            "occurrences_found": self.occurrences_found,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "sha256_before": self.sha256_before,
            "sha256_after": self.sha256_after,
            "changed": self.changed,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of one dry run or one execution.

    ``backup`` is not part of this value. The pre-execution capture is handed
    to the rollback provider through the executor's callback, so a caller that
    did not ask for a baseline never has one, and the baseline cannot be lost
    between the write and the recovery.
    """

    dry_run: bool
    applied: Tuple[ChangedFileEvidence, ...]
    refused: Tuple[str, ...]
    plan_size: int
    repository_name: str
    mechanism: str

    @property
    def changed_files(self) -> Tuple[str, ...]:
        """Repository-relative paths whose content actually changed."""

        return tuple(entry.path for entry in self.applied if entry.changed)

    @property
    def wrote_anything(self) -> bool:
        """True when a write actually happened on disk.

        Always false for a dry run, whatever its evidence reports: a dry run's
        evidence describes the change it *would* make.
        """

        return not self.dry_run and any(entry.changed for entry in self.applied)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable projection."""

        return {
            "dry_run": self.dry_run,
            "repository_name": self.repository_name,
            "mechanism": self.mechanism,
            "plan_size": self.plan_size,
            "changed_files": list(self.changed_files),
            "applied": [entry.to_dict() for entry in self.applied],
            "refused": list(self.refused),
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


class MigrationExecutor:
    """Apply a validated, bounded plan to one operator-approved repository.

    Args:
        approved_root: The repository root, already resolved by the existing
            :class:`~app.security.repository_input.RepositoryBoundary`.
        known_files: The repository-relative labels the existing bounded reader
            admitted. A plan may touch nothing else.
        backup: Optional callback invoked as ``backup(relative_path, text)``
            with each file's *original* content, immediately before that file is
            written. The recovery provider in :mod:`app.execution.rollback` uses
            it to capture a controlled baseline as part of the same execution, so
            a rollback can never race the write it is meant to undo.
    """

    def __init__(
        self,
        approved_root: Path,
        known_files: Sequence[str],
        *,
        backup: Optional[_BACKUP_CALLBACK] = None,
    ) -> None:
        root = Path(approved_root).resolve()
        if not root.is_dir():
            raise MigrationExecutionError(
                "the approved repository root must be an existing directory"
            )
        self._root = root
        # Normalise every admitted label against the root, so a plan may spell a
        # known file either way and still reach the same file. ``root / label``
        # is what makes this a root-relative resolution: resolving the bare label
        # would resolve it against the process's working directory instead.
        self._known = frozenset(
            (root / str(label)).resolve().relative_to(root).as_posix()
            for label in known_files
            if (root / str(label)).is_file()
        )
        self._backup = backup

    @property
    def repository_name(self) -> str:
        """The root's final path segment. A name, never a path."""

        return self._root.name

    @property
    def known_files(self) -> Tuple[str, ...]:
        """Every file a plan may touch, in deterministic order."""

        return tuple(sorted(self._known))

    def dry_run(self, plan: Any) -> ExecutionResult:
        """Report what a plan would change without writing anything.

        Every gate runs, including the precondition check, so a dry run is a
        real answer about the current filesystem state rather than a promise.

        Returns:
            An :class:`ExecutionResult` with ``dry_run=True`` whose evidence
            carries the digests the write would produce.

        Raises:
            MigrationExecutionError: If the plan itself is malformed, or if any
                target fails containment or its precondition. A dry run reports
                a plan that cannot be applied now as an error rather than a
                partial success.
        """

        return self._apply(plan, dry_run=True)

    def execute(self, plan: Any) -> ExecutionResult:
        """Apply a plan to the approved repository.

        The plan is validated in full and every precondition is checked against
        the live filesystem *before* the first write. If any target fails, the
        repository is left untouched and the error names the target.

        Returns:
            An :class:`ExecutionResult` with post-write digests measured by
            re-reading each file from disk, not by trusting the intended text.

        Raises:
            MigrationExecutionError: If the plan is malformed, a target is not
                a known file, a target escapes the root, or the on-disk content
                does not match the plan's expectation.
        """

        return self._apply(plan, dry_run=False)

    # -- internals ---------------------------------------------------------

    def _apply(self, plan: Any, *, dry_run: bool) -> ExecutionResult:
        operations = require_file_operations(plan)
        prepared = [self._prepare(operation) for operation in operations]

        if dry_run:
            return ExecutionResult(
                dry_run=True,
                applied=tuple(
                    self._measure(operation, before, expected_after, applied=None)
                    for operation, path, before, expected_after in prepared
                ),
                refused=(),
                plan_size=len(operations),
                repository_name=self.repository_name,
                mechanism="dry_run",
            )

        # Nothing has been written yet, so this is the last point at which an
        # unapplicable plan can be abandoned. The baseline is captured here, in
        # the same pass as the write, so a rollback can never race the change it
        # is meant to undo.
        for operation, path, before, expected_after in prepared:
            if self._backup is not None:
                self._backup(operation.path, before)
            self._write(path, expected_after)

        evidence = []
        for operation, path, before, expected_after in prepared:
            # Read the file back from disk. The evidence below reports what the
            # repository now contains, not what the plan intended, and a write
            # that did not land is an error rather than a quiet success.
            applied = self._read(path)
            if applied != expected_after:
                raise MigrationExecutionError(
                    f"{operation.label} did not read back as written, so the "
                    "change is not reported as successful"
                )
            evidence.append(
                self._measure(operation, before, expected_after, applied=applied)
            )

        return ExecutionResult(
            dry_run=False,
            applied=tuple(evidence),
            refused=(),
            plan_size=len(operations),
            repository_name=self.repository_name,
            mechanism=RESTORE_MECHANISM,
        )

    def _prepare(self, operation: FileOperation) -> Tuple[FileOperation, Path, str, str]:
        """Resolve one target and check its precondition. Writes nothing."""

        path = self._resolve(operation)
        before = self._read(path)
        found = before.count(operation.old_content)
        if found != operation.expected_occurrences:
            raise MigrationExecutionError(
                f"{operation.label} does not match the plan: expected "
                f"{operation.expected_occurrences} occurrence(s) of the expected "
                f"content but found {found}; the file was not changed"
            )
        return operation, path, before, before.replace(
            operation.old_content, operation.new_content
        )

    def _resolve(self, operation: FileOperation) -> Path:
        """Resolve one target, refusing anything outside the approved root."""

        if operation.path not in self._known:
            raise MigrationExecutionError(
                f"{operation.label} is not a file the bounded repository read "
                "admitted, so it cannot be changed"
            )
        candidate = (self._root / operation.path).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise MigrationExecutionError(
                f"{operation.label} resolves outside the approved repository root"
            )
        if not candidate.is_file():
            raise MigrationExecutionError(
                f"{operation.label} is not an existing file in the approved repository"
            )
        return candidate

    def _read(self, path: Path) -> str:
        """Read a file verbatim, so newline style survives unchanged."""

        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError) as error:
            raise MigrationExecutionError(
                f"{self._relative(path)} could not be read as UTF-8 text"
            ) from error

    def _write(self, path: Path, text: str) -> None:
        """Replace a file's content atomically, preserving newline style."""

        handle = None
        temporary = None
        try:
            # Same directory, so the move stays on one filesystem and is a
            # rename rather than a copy.
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".upshift-", suffix=".tmp", dir=str(path.parent)
            )
            temporary = Path(temporary_name)
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            raise MigrationExecutionError(
                f"{self._relative(path)} could not be written"
            ) from error
        finally:
            if handle is not None:
                handle.close()
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)

    def _measure(
        self,
        operation: FileOperation,
        before: str,
        expected_after: str,
        *,
        applied: Optional[str],
    ) -> ChangedFileEvidence:
        """Build one file's evidence, taking "after" from the filesystem.

        ``before`` was read before the write and ``applied`` is either the text
        read back from disk after it, or ``None`` for a dry run where the only
        honest "after" is the text the write would produce. Neither value is
        taken on trust from the plan.
        """

        after = expected_after if applied is None else applied
        return ChangedFileEvidence(
            path=operation.path,
            occurrences_expected=operation.expected_occurrences,
            occurrences_found=before.count(operation.old_content),
            bytes_before=len(before.encode("utf-8", "surrogatepass")),
            bytes_after=len(after.encode("utf-8", "surrogatepass")),
            sha256_before=_digest(before),
            sha256_after=_digest(after),
            changed=before != after,
        )

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()
