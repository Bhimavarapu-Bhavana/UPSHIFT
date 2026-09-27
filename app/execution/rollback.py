"""A controlled baseline for a real repository, so the existing engine can roll back.

The Phase 9 :class:`~app.core.recovery_engine.MigrationRecoveryEngine` already
knows how to roll a failed migration back and re-verify the restored baseline.
What it lacked was a provider for a real repository: until now the only provider
copied a controlled benchmark package into a temporary workspace.

This module supplies that provider, and it does so without changing the engine.

Where the baseline comes from
----------------------------
The executor captures each file's content *immediately before* it writes, as
part of the same execution pass, and hands it to
:class:`BaselineRollbackProvider.capture`. The capture therefore cannot race the
write it is meant to undo, and it only ever contains files that were actually
about to change.

The captured text lives in memory for the lifetime of one task and is never
persisted. A restart therefore cannot restore a baseline, and
:func:`rollback_available` reports that honestly instead of pretending. The
alternative, writing pre-execution copies into the repository, would put a
second copy of user content next to the real one for no benefit.

What verification of a restored baseline means here
---------------------------------------------------
UPSHIFT never executes repository code, so it cannot observe behaviour by
running a repository. Instead the verification candidate resolves by *reading*
the restored file and returning its content. A rollback case's expectation is
the content captured before execution, so ``PASS`` means the file on disk is
byte-identical to the pre-migration state. That is a real filesystem fact, and
it is the only claim made.

Bounded by construction
-----------------------
The existing engine performs at most one restore and at most one baseline
verification, and this provider adds no retry of its own. Restoring a file that
was not captured, or whose current content is not the content this provider
wrote, is refused rather than forced.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from app.core.recovery_engine import RollbackResult
from app.verification.verifier import VerificationCandidate

__all__ = [
    "BASELINE_CANDIDATE_ID",
    "BaselineRollbackProvider",
    "RESTORED_CANDIDATE_ID",
    "RESTORE_MECHANISM",
    "file_content_candidate",
    "file_text_case",
]

#: Candidate label reported for the pre-execution state.
BASELINE_CANDIDATE_ID = "pre_execution_baseline"

#: Candidate label reported after a successful restore.
RESTORED_CANDIDATE_ID = "restored_baseline"

#: Reported as the restoration mechanism, matching what the executor does.
RESTORE_MECHANISM = "pre_execution_content_capture"


def _digest(text: str) -> str:
    """Content digest used to detect an edit this run did not make."""

    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def file_text_case(case_id: str, relative_path: str, expected: str, *, description: str) -> Any:
    """Build one verification case asserting a file's exact content.

    Args:
        case_id: Stable identifier, unique within the run.
        relative_path: The repository-relative file the case observes.
        expected: The exact text the file must contain.
        description: Human-readable explanation of the preserved behavior.

    Returns:
        A :class:`~app.verification.verifier.VerificationCase` whose input is
        the relative path and whose expectation is the text.
    """

    from app.verification.verifier import VerificationCase

    return VerificationCase(
        case_id=case_id,
        description=description,
        inputs=(relative_path,),
        expected=expected,
        required=True,
    )


def file_content_candidate(
    repository_name: str,
    relative_paths: Tuple[str, ...],
    read_text: Callable[[str], Optional[str]],
) -> VerificationCandidate:
    """Build a verification candidate that *reads* files instead of running them.

    Args:
        repository_name: A name for the repository, never a path.
        relative_paths: The files this candidate may read, from the bounded
            reader's admitted set.
        read_text: A narrow callable that returns one file's text, or ``None``
            when it cannot be read. It is supplied by the caller and is never
            built from repository content.

    Returns:
        A :class:`~app.verification.verifier.VerificationCandidate` whose
        ``resolve`` accepts a relative path from ``relative_paths`` and returns
        that file's current text.
    """

    allowed = frozenset(relative_paths)

    def resolve(relative_path: Any) -> str:
        if not isinstance(relative_path, str) or relative_path not in allowed:
            # A path outside the admitted set is reported as missing text rather
            # than read. Nothing here can reach a file the bounded read refused.
            return ""
        text = read_text(relative_path)
        return "" if text is None else text

    return VerificationCandidate(
        candidate_id=RESTORED_CANDIDATE_ID,
        path=repository_name,
        resolve=resolve,
    )


class BaselineRollbackProvider:
    """Restore files captured immediately before the migration wrote them.

    Implements the existing :class:`~app.core.recovery_engine.RollbackProvider`
    protocol: ``rollback_available``, ``restore_baseline``, and
    ``load_restored_baseline``.
    """

    def __init__(self, approved_root: Path, *, mechanism: str = RESTORE_MECHANISM) -> None:
        self._root = Path(approved_root).resolve()
        self._mechanism = mechanism
        # ``(relative_path, original_text)`` in capture order.
        self._baseline: Dict[str, str] = {}
        # What this provider last wrote, so a restore can refuse to clobber an
        # edit it did not make.
        self._applied: Dict[str, str] = {}
        self._restored = False

    @property
    def mechanism(self) -> str:
        """The restoration mechanism, as a fact."""

        return self._mechanism

    @property
    def captured_files(self) -> Tuple[str, ...]:
        """The files a restore would touch, in capture order."""

        return tuple(self._baseline)

    def capture(self, relative_path: str, original_text: str) -> None:
        """Record one file's pre-execution content.

        Called by the executor immediately before writing, so the stored value
        is the state the write is about to replace. A second capture for the
        same file is refused: a plan may touch a file once, so a duplicate means
        the baseline would be ambiguous.
        """

        if relative_path in self._baseline:
            raise ValueError(
                f"{relative_path} was already captured, so its pre-execution "
                "content would be ambiguous"
            )
        self._baseline[relative_path] = original_text

    def rollback_available(self, candidate_id: str) -> bool:
        """True when this provider captured something it could restore."""

        if not self._baseline:
            return False
        if self._restored:
            # A second attempt is not offered. The engine asks once; a provider
            # that kept saying yes would turn a bounded recovery into a loop.
            return False
        return isinstance(candidate_id, str) and bool(candidate_id)

    def restore_baseline(self, candidate_id: str) -> RollbackResult:
        """Write every captured file back to its pre-execution content.

        Every target is checked before any is written, so a refusal leaves the
        repository exactly as the failed migration left it rather than half
        restored.
        """

        if not self.rollback_available(candidate_id):
            return RollbackResult.unavailable(
                "no pre-execution baseline was captured, or a restore was "
                "already performed, so no controlled restoration is available"
            )

        targets = []
        for relative_path, original in self._baseline.items():
            path = self._resolve(relative_path)
            if relative_path in self._applied:
                current = self._read(path)
                if _digest(current) != self._applied[relative_path]:
                    return RollbackResult.failure(
                        f"{relative_path} no longer contains the change this "
                        "migration applied, so it was not restored; refusing to "
                        "overwrite an edit this run did not make"
                    )
            targets.append((path, relative_path, original))

        try:
            for path, _relative_path, original in targets:
                with path.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(original)
        except OSError as error:
            return RollbackResult.failure(
                f"a captured file could not be restored: {type(error).__name__}"
            )

        self._restored = True
        return RollbackResult(
            attempted=True,
            succeeded=True,
            reason=(
                f"restored {len(targets)} file(s) to the content captured "
                "immediately before the migration wrote them"
            ),
            restored_candidate_id=RESTORED_CANDIDATE_ID,
            mechanism=self._mechanism,
        )

    def record_applied(self, evidence: Any) -> None:
        """Note the post-write digest of each changed file, for the clobber check.

        Args:
            evidence: An iterable of
                :class:`~app.execution.migration_executor.ChangedFileEvidence`,
                or a single one.
        """

        for entry in evidence:
            self._applied[entry.path] = entry.sha256_after

    def load_restored_baseline(self) -> VerificationCandidate:
        """Return a candidate that reads the restored files back.

        The comparison is by content, not by the digest recorded at write time,
        so the verification result reflects the repository as it is now.
        """

        def read_text(relative_path: str) -> Optional[str]:
            try:
                return self._read(self._resolve(relative_path))
            except MigrationBaselineError:
                return None

        return file_content_candidate(
            repository_name=self._root.name,
            relative_paths=tuple(self._baseline),
            read_text=read_text,
        )

    def baseline_cases(self) -> Tuple[Any, ...]:
        """Build the rollback verification cases from the captured content.

        Each case asserts one file's exact pre-execution text, so a successful
        rollback is proven byte-for-byte rather than by a summary line.
        """

        return tuple(
            file_text_case(
                case_id=f"rollback_restored:{relative_path}",
                relative_path=relative_path,
                expected=original,
                description=(
                    f"After rollback, {relative_path} must again contain the "
                    "exact content it had before the migration was executed."
                ),
            )
            for relative_path, original in self._baseline.items()
        )

    # -- internals ---------------------------------------------------------

    def _resolve(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path:
            raise MigrationBaselineError("a captured path must be non-empty text")
        if relative_path.startswith(("/", "\\")) or ":" in relative_path:
            raise MigrationBaselineError(
                "a captured path must be relative, so it cannot escape the root"
            )
        parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise MigrationBaselineError(
                "a captured path must not contain a '.' or '..' component"
            )
        candidate = (self._root / Path(*parts)).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise MigrationBaselineError(
                "a captured path resolves outside the approved repository root"
            )
        return candidate

    def _read(self, path: Path) -> str:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()


class MigrationBaselineError(ValueError):
    """Raised when a captured path cannot be resolved safely."""
