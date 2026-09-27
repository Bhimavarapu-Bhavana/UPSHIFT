"""Controlled, deterministic baseline restoration for the migration benchmark.

This module provides the single concrete :class:`RollbackProvider` used by the
Phase 9 recovery demonstration. It is deliberately narrow and benchmark-scoped.

How restoration works, and why it is safe
-----------------------------------------

The provider never edits the repository. It never moves, deletes, or rewrites
the protected OLD baseline. Instead it performs a **controlled restoration into
a temporary working copy**:

1. The known-good OLD baseline source is *read* from the controlled benchmark.
2. That exact text is written into a fresh directory created by
   :mod:`tempfile` outside the repository.
3. The restored copy is loaded in-process and handed to the existing Phase 6
   verifier as a narrow callable.

The protected baseline is only ever opened for reading, so the demonstration
cannot permanently alter it. The temporary working copy is created on demand
and removed by :meth:`BenchmarkRollbackProvider.close`.

Deliberate restrictions
-----------------------

* ``candidate_id`` is validated against the statically known benchmark
  candidates. An unknown identifier is refused.
* There is no ``command``, ``args``, ``script``, or ``callable`` parameter, so
  no shell command or Python source can be supplied for execution.
* The workspace location is chosen by this provider, never by the caller, and
  it is always outside the repository.
* No Git operation, no force operation, no network access, and no credentials.
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Optional, Tuple

from ..core.recovery_engine import RollbackResult
from ..verification.verifier import UnknownCandidateError, VerificationCandidate
from .profile_label_benchmark import KNOWN_CANDIDATES

__all__ = [
    "BASELINE_CANDIDATE_ID",
    "RESTORED_CANDIDATE_ID",
    "RESTORE_MECHANISM",
    "WORKSPACE_KIND",
    "BenchmarkRollbackProvider",
]

#: The statically known known-good candidate that restoration copies from.
BASELINE_CANDIDATE_ID = "old_baseline"

#: The identifier reported for the restored baseline in the temporary copy.
RESTORED_CANDIDATE_ID = "restored_baseline"

#: Human-readable, path-free description of the restoration mechanism.
RESTORE_MECHANISM = "controlled_restore_to_temporary_working_copy"

#: Stable label for the temporary working copy, used in deterministic evidence.
WORKSPACE_KIND = "temporary working copy"

_KNOWN_IDS: Tuple[str, ...] = tuple(spec.candidate_id for spec in KNOWN_CANDIDATES)
_BASELINE_SPEC = next(
    spec for spec in KNOWN_CANDIDATES if spec.candidate_id == BASELINE_CANDIDATE_ID
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_PACKAGE_DIR = _REPOSITORY_ROOT / "demo" / "migration_benchmark" / "old"

#: The restored baseline is materialized as a real package under a fixed name so
#: the baseline's own relative imports resolve inside the working copy. The name
#: is constant, which keeps restoration deterministic.
_RESTORED_PACKAGE_NAME = "upshift_restored_baseline"
_RESTORED_MODULE_NAME = f"{_RESTORED_PACKAGE_NAME}.profile_service"

#: Path-free labels used in deterministic report evidence.
WORKSPACE_KIND = "temporary working copy"
_RESTORED_PATH_LABEL = f"{WORKSPACE_KIND}/{_RESTORED_PACKAGE_NAME}/profile_service.py"


class BenchmarkRollbackProvider:
    """Restores the known-good baseline into a temporary working copy.

    The provider is a plain object rather than a Protocol implementation so it
    can be used with a ``with`` statement, which guarantees the temporary
    working copy is removed even if recovery raises.
    """

    def __init__(self, *, available: bool = True) -> None:
        """Create a provider.

        ``available=False`` models an environment where no controlled
        restoration mechanism exists, which drives the REPLANNING path.
        """

        self._available = bool(available)
        self._workspace: Optional[Path] = None
        self._restored_package: Optional[Path] = None
        self._loaded_module: Optional[ModuleType] = None
        self.restore_calls = 0
        self.load_calls = 0

    # -- controlled provider surface --------------------------------------

    def rollback_available(self, candidate_id: str) -> bool:
        """Report whether a controlled restoration exists for this candidate."""

        self._require_known_candidate(candidate_id)
        if not self._available:
            return False
        return self._baseline_sources() is not None

    def restore_baseline(self, candidate_id: str) -> RollbackResult:
        """Copy the known-good baseline into a temporary working copy."""

        self._require_known_candidate(candidate_id)
        self.restore_calls += 1

        if not self._available:
            return RollbackResult.unavailable(
                "No controlled rollback mechanism is configured for this benchmark run."
            )

        sources = self._baseline_sources()
        if sources is None:
            return RollbackResult.failure(
                "The known-good baseline source is not present in the benchmark."
            )

        try:
            workspace = Path(tempfile.mkdtemp(prefix="upshift-recovery-"))
            package = workspace / _RESTORED_PACKAGE_NAME
            package.mkdir(parents=True, exist_ok=True)
            for name, text in sources:
                (package / name).write_text(text, encoding="utf-8")
        except OSError as error:
            return RollbackResult.failure(
                f"The {WORKSPACE_KIND} could not be prepared "
                f"({type(error).__name__})"
            )

        # Only publish the new workspace once it is fully written.
        self._discard_workspace()
        self._workspace = workspace
        self._restored_package = workspace / _RESTORED_PACKAGE_NAME
        self._loaded_module = None

        return RollbackResult(
            attempted=True,
            succeeded=True,
            reason=(
                f"Restored the known-good {BASELINE_CANDIDATE_ID!r} baseline, including "
                "its baseline dependency module, into an isolated "
                f"{WORKSPACE_KIND} outside the repository; the protected baseline "
                "sources were read only and left unchanged."
            ),
            restored_candidate_id=RESTORED_CANDIDATE_ID,
            mechanism=RESTORE_MECHANISM,
        )

    def load_restored_baseline(self) -> VerificationCandidate:
        """Return the restored baseline as a narrow in-process callable."""

        self.load_calls += 1
        if self._restored_package is None or not self._restored_package.is_dir():
            raise UnknownCandidateError(
                "no restored baseline is available; call restore_baseline first"
            )

        module = self._import_restored_module()
        factory = getattr(module, _BASELINE_SPEC.class_name)
        method = getattr(factory(), _BASELINE_SPEC.method_name)

        def resolve(*inputs: object) -> object:
            return method(*inputs)

        resolve.__name__ = _BASELINE_SPEC.method_name
        # A stable, path-free label keeps recovery reports deterministic and
        # free of temporary-directory noise.
        return VerificationCandidate(
            candidate_id=RESTORED_CANDIDATE_ID,
            path=_RESTORED_PATH_LABEL,
            resolve=resolve,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Remove the temporary working copy. Never touches the repository."""

        self._discard_workspace()

    def __enter__(self) -> "BenchmarkRollbackProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _require_known_candidate(candidate_id: str) -> None:
        if candidate_id not in _KNOWN_IDS:
            known = ", ".join(sorted(_KNOWN_IDS))
            raise UnknownCandidateError(
                f"unknown candidate {candidate_id!r}; known candidates: {known}"
            )

    @staticmethod
    def _baseline_sources() -> Optional[Tuple[Tuple[str, str], ...]]:
        """Read the known-good baseline package, or None when it is absent.

        Returns the ``(filename, text)`` pairs of every ``.py`` file in the
        baseline package, in sorted order, so restoration is deterministic. The
        files are opened read-only.
        """

        if not _BASELINE_PACKAGE_DIR.is_dir():
            return None
        sources = []
        for path in sorted(_BASELINE_PACKAGE_DIR.glob("*.py")):
            try:
                sources.append((path.name, path.read_text(encoding="utf-8")))
            except OSError:
                return None
        return tuple(sources) or None

    def _import_restored_module(self) -> ModuleType:
        """Import the restored copy from the temporary working copy.

        The temporary workspace is placed on ``sys.path`` only for the duration
        of the import, and every module it registered is removed afterwards, so
        repeated restorations cannot accumulate state.
        """

        assert self._restored_package is not None
        workspace = str(self._workspace)
        self._purge_restored_modules()
        sys.path.insert(0, workspace)
        try:
            module = importlib.import_module(_RESTORED_MODULE_NAME)
        except Exception:
            self._purge_restored_modules()
            raise
        finally:
            try:
                sys.path.remove(workspace)
            except ValueError:
                pass
        return module

    @staticmethod
    def _purge_restored_modules() -> None:
        prefix = f"{_RESTORED_PACKAGE_NAME}."
        for name in [
            name for name in sys.modules if name == _RESTORED_PACKAGE_NAME or name.startswith(prefix)
        ]:
            del sys.modules[name]

    def _discard_workspace(self) -> None:
        self._purge_restored_modules()
        if self._workspace is not None:
            shutil.rmtree(self._workspace, ignore_errors=True)
        self._workspace = None
        self._restored_package = None
        self._loaded_module = None
