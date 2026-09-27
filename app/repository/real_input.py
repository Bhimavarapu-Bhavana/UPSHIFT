"""Bounded, read-only repository reader for UPSHIFT real-repository mode.

This module is the input adapter. It turns one already-validated repository
directory into deterministic evidence plus a bounded snapshot of text, and
hands both to the existing Phase 4-9 engines. It performs no analysis of its
own: symbol search, risk scoring, verification, the decision, and recovery all
remain where they already are.

What it does
------------

* Walks the validated repository once, breadth-first, in sorted name order.
* Reads only text files whose extension the Phase 4 analyzer can search.
* Enforces an explicit per-file, per-run byte, file-count, and directory budget.
* Detects binary content instead of guessing at it, and records the reason.
* Refuses credential, key, and environment-file names outright.
* Does not follow symbolic links or Windows junctions, and re-checks that every
  entry it is about to read still resolves inside the validated root.
* Records exactly which files were inspected, and why each excluded file was
  excluded, so the evidence is auditable.

What it never does
------------------

* It never executes, imports, compiles, or evaluates repository content. Text is
  read as text; nothing in a repository is ever run.
* It never spawns a process, opens a socket, or invokes Git.
* It never writes, renames, or deletes anything. The repository is opened
  read-only and is unchanged when the read finishes.
* It never reads a credential, private key, token, or environment file: those
  names are refused before the file is opened.
* It never expands ``~`` or consults an environment variable to find a path.
  The root arrives already resolved from :mod:`app.security.repository_input`.

The adapter is deliberately narrow, so a caller cannot turn it into a general
filesystem reader: :class:`RepositoryInput` exposes exactly one operation,
:meth:`RepositoryInput.load`, and a load reads text only.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from app.core.impact_analyzer import EXCLUDED_DIRECTORY_NAMES, TEXT_SUFFIXES
from app.core.recovery_engine import RollbackResult
from app.repository.evidence import (
    DEFAULT_LIMITS,
    MAX_RECORDED_INSPECTED_FILES,
    MAX_RECORDED_SKIPS_PER_REASON,
    SKIP_BINARY,
    SKIP_BYTE_BUDGET,
    SKIP_DIRECTORY_BUDGET,
    SKIP_FILE_BUDGET,
    SKIP_OUTSIDE_BOUNDARY,
    SKIP_OVERSIZED,
    SKIP_SENSITIVE,
    SKIP_SYMLINK,
    SKIP_UNREADABLE,
    SKIP_UNSUPPORTED_TYPE,
    InspectedFile,
    RepositoryEvidence,
    RepositoryLimits,
    RepositoryLoad,
    SkippedSummary,
)
from app.security.repository_input import MODE_REAL_REPOSITORY
from app.verification.verifier import VerificationCandidate

__all__ = [
    "DIRECTORY_EXCLUSIONS",
    "NO_CONTROLLED_ROLLBACK",
    "SENSITIVE_FILE_NAMES",
    "SENSITIVE_FILE_SUFFIXES",
    "SENSITIVE_NAME_FRAGMENTS",
    "NoControlledRollbackProvider",
    "RealRepositoryInput",
    "RepositoryInput",
    "RepositoryReadOnlyError",
]

#: Generated, cached, and version-control directories that are never walked.
#: The Phase 4 exclusions are reused so both analysis modes ignore the same
#: directories; the remainder cover other common tool caches.
DIRECTORY_EXCLUSIONS = frozenset(EXCLUDED_DIRECTORY_NAMES) | frozenset(
    {
        ".cache",
        ".eggs",
        ".gradle",
        ".hg",
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".serverless",
        ".svn",
        ".terraform",
        "Pods",
        "coverage",
        "venv",
    }
)

#: Exact file names that are never read, whatever their extension.
SENSITIVE_FILE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "_netrc",
        "authorized_keys",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
    }
)

#: Extensions that only ever carry keys, certificates, or keystores.
SENSITIVE_FILE_SUFFIXES = frozenset(
    {
        ".asc",
        ".cer",
        ".crt",
        ".gpg",
        ".jks",
        ".kdb",
        ".kdbx",
        ".key",
        ".keystore",
        ".p12",
        ".pem",
        ".pfx",
        ".pgp",
        ".ppk",
    }
)

#: A name containing any of these is treated as sensitive and left alone. The
#: match is deliberately broad: refusing a file that turned out to be harmless
#: costs an unread reference, while reading a real key costs a secret.
SENSITIVE_NAME_FRAGMENTS = ("credential", "password", "secret")

#: Bytes inspected when deciding whether content is binary.
_BINARY_PROBE_BYTES = 8192

#: Reported mechanism for a real-repository read, which restores nothing.
NO_CONTROLLED_ROLLBACK = "no_controlled_baseline_for_analyzed_repository"


class RepositoryReadOnlyError(RuntimeError):
    """Raised when something asks a repository read to act instead of observe."""


class _Binary:
    """Sentinel for content that is not text and must not be analyzed."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "BINARY"


_BINARY = _Binary()


@runtime_checkable
class RepositoryInput(Protocol):
    """The one operation every repository input adapter exposes.

    An adapter turns an input into a bounded read. It has no other method, so it
    cannot be asked to write, execute, fetch, or restore anything.
    """

    mode: str

    def load(self) -> RepositoryLoad:
        """Read the input and return its evidence and bounded text snapshot."""


def _is_excluded_directory(name: str) -> bool:
    return name in DIRECTORY_EXCLUSIONS or name.endswith(".egg-info")


def _is_sensitive_file(name: str) -> bool:
    """Refuse a file whose name suggests credentials, keys, or environment data.

    Both the full name and the bare stem are matched, so ``id_rsa`` and
    ``id_rsa.pub`` are both refused rather than only the extension-less form.
    """

    lowered = name.lower()
    stem = Path(lowered).stem
    if lowered in SENSITIVE_FILE_NAMES or stem in SENSITIVE_FILE_NAMES:
        return True
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if Path(name).suffix.lower() in SENSITIVE_FILE_SUFFIXES:
        return True
    return any(fragment in lowered for fragment in SENSITIVE_NAME_FRAGMENTS)


def _is_searchable_text(name: str) -> bool:
    return Path(name).suffix.lower() in TEXT_SUFFIXES


def _is_junction(entry: Path) -> bool:
    """Report a Windows junction, which is not reported as a symlink."""

    try:
        return bool(entry.is_junction())
    except AttributeError:  # pragma: no cover - Python without junction support
        return False


class RealRepositoryInput:
    """Reads one validated local repository, read-only and size-bounded."""

    mode = MODE_REAL_REPOSITORY

    def __init__(
        self,
        repository_root: Path,
        *,
        limits: Optional[RepositoryLimits] = None,
    ) -> None:
        """Bind the adapter to an already-validated repository root.

        Args:
            repository_root: The resolved root produced by
                :class:`app.security.repository_input.RepositoryBoundary`. This
                class does not re-derive trust from the path; it re-checks
                containment for every entry it reads.
            limits: Optional read budgets. Defaults to
                :data:`app.repository.evidence.DEFAULT_LIMITS`.
        """

        if not isinstance(repository_root, Path):
            raise RepositoryReadOnlyError("repository_root must be a resolved Path")
        if not repository_root.is_dir():
            raise RepositoryReadOnlyError("repository_root must be an existing directory")

        # Resolved once, and only to make the containment test compare like with
        # like. Every entry is compared against the root through
        # ``Path.resolve()``, which returns the long form of a path. A root
        # supplied in an equivalent short form - which is what
        # ``tempfile.gettempdir()`` yields on Windows, where a user profile
        # appears as ``C:\\Users\\BHIMAV~1\\...`` - would otherwise fail
        # ``relative_to`` for every single entry, and this reader would report a
        # complete, successful, empty read. Normalizing here keeps containment
        # strict while removing that silent-empty-result failure mode. For an
        # already-resolved root, as the boundary always supplies, this is a
        # no-op.
        try:
            resolved_root = repository_root.resolve()
        except (OSError, ValueError) as error:
            raise RepositoryReadOnlyError(
                "repository_root could not be resolved to a local filesystem path"
            ) from error

        self._root = resolved_root
        self._limits = limits if limits is not None else DEFAULT_LIMITS

    @property
    def repository_root(self) -> Path:
        return self._root

    # -- reading ------------------------------------------------------------

    def load(self) -> RepositoryLoad:
        """Read the repository once and return evidence plus bounded text.

        The walk is breadth-first with sorted entries, and every result is sorted
        before it is returned, so two reads of an unchanged repository produce
        identical evidence and identical text.
        """

        inspected: List[InspectedFile] = []
        files: List[Tuple[str, str]] = []
        skip_counts: Dict[str, int] = {}
        skip_paths: Dict[str, List[str]] = {}
        ignored: Dict[str, int] = {}
        total_bytes = 0
        file_budget = self._limits.max_files
        byte_budget = self._limits.max_total_bytes
        directory_budget = self._limits.max_directories

        pending = deque((self._root,))
        visited_directories = 0

        while pending:
            directory = pending.popleft()
            if visited_directories >= directory_budget:
                self._note(skip_counts, skip_paths, SKIP_DIRECTORY_BUDGET, "<walk truncated>")
                break
            visited_directories += 1

            try:
                entries = sorted(directory.iterdir(), key=lambda item: item.name)
            except OSError:
                self._note(
                    skip_counts,
                    skip_paths,
                    SKIP_UNREADABLE,
                    self._relative(directory) or directory.name,
                )
                continue

            for entry in entries:
                try:
                    resolved = entry.resolve()
                except (OSError, ValueError):
                    self._note(skip_counts, skip_paths, SKIP_UNREADABLE, entry.name)
                    continue

                if entry.is_symlink() or _is_junction(entry):
                    self._note(
                        skip_counts, skip_paths, SKIP_SYMLINK, self._relative(resolved) or entry.name
                    )
                    continue

                relative = self._relative(resolved)
                if relative is None:
                    self._note(skip_counts, skip_paths, SKIP_OUTSIDE_BOUNDARY, entry.name)
                    continue

                if resolved.is_dir():
                    if _is_excluded_directory(entry.name):
                        ignored[entry.name] = ignored.get(entry.name, 0) + 1
                        continue
                    pending.append(resolved)
                    continue

                if not resolved.is_file():
                    self._note(skip_counts, skip_paths, SKIP_UNSUPPORTED_TYPE, relative)
                    continue

                if _is_sensitive_file(entry.name):
                    self._note(skip_counts, skip_paths, SKIP_SENSITIVE, relative)
                    continue

                if not _is_searchable_text(entry.name):
                    self._note(skip_counts, skip_paths, SKIP_UNSUPPORTED_TYPE, relative)
                    continue

                if len(files) >= file_budget:
                    self._note(skip_counts, skip_paths, SKIP_FILE_BUDGET, relative)
                    continue

                try:
                    size = resolved.stat().st_size
                except OSError:
                    self._note(skip_counts, skip_paths, SKIP_UNREADABLE, relative)
                    continue

                if size > self._limits.max_file_bytes:
                    self._note(skip_counts, skip_paths, SKIP_OVERSIZED, relative)
                    continue

                if total_bytes + size > byte_budget:
                    self._note(skip_counts, skip_paths, SKIP_BYTE_BUDGET, relative)
                    continue

                content = self._read_text(resolved)
                if content is None:
                    self._note(skip_counts, skip_paths, SKIP_UNREADABLE, relative)
                    continue
                if content is _BINARY:
                    self._note(skip_counts, skip_paths, SKIP_BINARY, relative)
                    continue

                files.append((relative, content))
                total_bytes += size
                inspected.append(
                    InspectedFile(
                        path=relative,
                        size_bytes=size,
                        line_count=len(content.splitlines()),
                    )
                )

        files.sort()
        inspected.sort(key=lambda entry: entry.path)

        evidence = RepositoryEvidence(
            repository_name=self._root.name,
            repository_path=str(self._root),
            inspected_files=tuple(inspected[:MAX_RECORDED_INSPECTED_FILES]),
            inspected_file_count=len(inspected),
            inspected_truncated=len(inspected) > MAX_RECORDED_INSPECTED_FILES,
            skipped=self._summaries(skip_counts, skip_paths),
            skipped_file_count=sum(skip_counts.values()),
            ignored_directories=tuple(sorted(ignored.items())),
            total_bytes_read=total_bytes,
            limits=self._limits,
            safety_restrictions=self._safety_restrictions(),
        )
        return RepositoryLoad(evidence=evidence, files=tuple(files))

    # -- internals ----------------------------------------------------------

    def _relative(self, path: Path) -> Optional[str]:
        """Return the repository-relative POSIX label, or ``None`` if outside.

        Containment is re-checked here for every single entry, so a directory
        tree cannot be walked out of the validated root even if an intermediate
        link appeared between validation and reading.
        """

        try:
            return path.relative_to(self._root).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _read_text(path: Path) -> Any:
        """Return the file's text, ``_BINARY``, or ``None`` when unreadable."""

        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
            return _BINARY
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _note(
        counts: Dict[str, int],
        paths: Dict[str, List[str]],
        reason: str,
        path: str,
    ) -> None:
        """Record one excluded entry, keeping counts exact and paths bounded."""

        counts[reason] = counts.get(reason, 0) + 1
        recorded = paths.setdefault(reason, [])
        if len(recorded) < MAX_RECORDED_SKIPS_PER_REASON:
            recorded.append(path)

    @staticmethod
    def _summaries(
        counts: Dict[str, int],
        paths: Dict[str, List[str]],
    ) -> Tuple[SkippedSummary, ...]:
        return tuple(
            SkippedSummary(
                reason=reason,
                count=counts[reason],
                recorded_paths=tuple(sorted(set(paths.get(reason, ())))),
                truncated=counts[reason] > MAX_RECORDED_SKIPS_PER_REASON,
            )
            for reason in sorted(counts)
        )

    def _safety_restrictions(self) -> Tuple[str, ...]:
        """The restrictions this read actually applied, as reportable facts."""

        return (
            "read-only: no repository file is created, modified, renamed, or deleted",
            "no repository file is executed, imported, compiled, or evaluated",
            "no shell, subprocess, or Python execution of any kind",
            "no network access and no Git command execution",
            "credential, private-key, and environment files are never read",
            "only text files with an analyzable extension are read "
            f"({len(TEXT_SUFFIXES)} extensions)",
            f"files larger than {self._limits.max_file_bytes} bytes are skipped",
            f"at most {self._limits.max_files} files and "
            f"{self._limits.max_total_bytes} bytes are read per repository",
            "symbol search runs only over the bounded text snapshot this read produced",
        )


class NoControlledRollbackProvider:
    """Rollback provider for a repository UPSHIFT only reads.

    A real repository has no controlled baseline in this design: UPSHIFT does
    not migrate it, so it has no prior state it could safely restore. Reporting
    the mechanism as unavailable is therefore not a special case. In practice
    the existing Phase 9 recovery engine never reaches a rollback attempt in
    this mode at all: verification is INCONCLUSIVE by design, and the engine
    records that no acceptance and no rollback are justified. This provider
    makes that outcome structural rather than incidental, so a future change
    cannot quietly start restoring an analyzed repository.

    The provider cannot restore anything, and it exposes no command, path, or
    callable to do so.
    """

    def rollback_available(self, candidate_id: str) -> bool:
        """Always false: an analyzed repository has no controlled baseline."""

        return False

    def restore_baseline(self, candidate_id: str) -> RollbackResult:
        """Report the mechanism as unavailable rather than attempting anything."""

        return RollbackResult.unavailable(
            "UPSHIFT only reads an analyzed repository, so no controlled baseline "
            "restoration exists for it, and nothing was changed."
        )

    def load_restored_baseline(self) -> VerificationCandidate:
        """Refuse: there is no restored baseline to load."""

        raise RepositoryReadOnlyError(
            "no restored baseline exists for a repository UPSHIFT only reads"
        )
