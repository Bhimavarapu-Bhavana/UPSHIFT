"""Immutable evidence describing what a repository read actually covered.

These types are the audit trail for real-repository mode. Every value is a
plain frozen scalar or a tuple of them, so an evidence object can be rendered
in a response, compared between runs, and asserted in a test without holding a
filesystem handle, a directory, or a callable.

Security boundary enforced here:

* Evidence is descriptive only. It carries no command, no callable, and no
  object that could reach back into the system.
* File locations are repository-relative POSIX labels, produced by the reader
  that already validated the repository boundary. Nothing here re-resolves a
  path or re-checks containment.
* Counts are exact. The recorded path lists are capped, and a cap is reported
  explicitly through ``truncated`` rather than being hidden by omission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

__all__ = [
    "DEFAULT_LIMITS",
    "MAX_RECORDED_SKIPS_PER_REASON",
    "MAX_RECORDED_INSPECTED_FILES",
    "SKIP_BINARY",
    "SKIP_BYTE_BUDGET",
    "SKIP_DIRECTORY_BUDGET",
    "SKIP_FILE_BUDGET",
    "SKIP_OUTSIDE_BOUNDARY",
    "SKIP_OVERSIZED",
    "SKIP_SENSITIVE",
    "SKIP_SYMLINK",
    "SKIP_UNREADABLE",
    "SKIP_UNSUPPORTED_TYPE",
    "SKIP_REASONS",
    "InspectedFile",
    "RepositoryEvidence",
    "RepositoryLimits",
    "RepositoryLoad",
    "SkippedSummary",
]

#: The file was readable text, but not a type migration analysis can use.
SKIP_UNSUPPORTED_TYPE = "unsupported_file_type"
#: The file was large enough that reading it would exceed the safe read budget.
SKIP_OVERSIZED = "oversized_file"
#: The content is binary, so no textual symbol search is meaningful.
SKIP_BINARY = "binary_content"
#: The name matches a credential, key, or environment-file pattern.
SKIP_SENSITIVE = "sensitive_file"
#: The entry is a symbolic link or a Windows junction, so it was not followed.
SKIP_SYMLINK = "link_not_followed"
#: The entry resolved outside the repository root that was validated.
SKIP_OUTSIDE_BOUNDARY = "outside_repository_boundary"
#: The entry could not be read, so it contributed no evidence either way.
SKIP_UNREADABLE = "unreadable"
#: The per-run file budget was exhausted before this entry was reached.
SKIP_FILE_BUDGET = "file_budget_exhausted"
#: The per-run byte budget was exhausted before this entry was read.
SKIP_BYTE_BUDGET = "byte_budget_exhausted"
#: The per-run directory budget was exhausted before the walk could descend.
SKIP_DIRECTORY_BUDGET = "directory_budget_exhausted"

#: Every reason a file can be left out of the analysed snapshot, in stable order.
SKIP_REASONS: Tuple[str, ...] = (
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
)

#: Longest recorded skip list per reason. Counts stay exact; only the sample of
#: paths shown in a response is capped, and the cap is reported.
MAX_RECORDED_SKIPS_PER_REASON = 100

#: Longest recorded list of inspected files in one response.
MAX_RECORDED_INSPECTED_FILES = 500


@dataclass(frozen=True)
class RepositoryLimits:
    """Explicit, conservative read budgets for one repository read.

    The defaults are deliberately small enough that a read cannot be turned into
    a bulk copy, and large enough for ordinary source trees.
    """

    max_file_bytes: int = 262_144
    max_total_bytes: int = 33_554_432
    max_files: int = 2_000
    max_directories: int = 4_000

    def to_dict(self) -> Tuple[Tuple[str, int], ...]:
        return (
            ("max_file_bytes", self.max_file_bytes),
            ("max_total_bytes", self.max_total_bytes),
            ("max_files", self.max_files),
            ("max_directories", self.max_directories),
        )


#: The limits applied when a caller does not supply its own.
DEFAULT_LIMITS = RepositoryLimits()


@dataclass(frozen=True)
class InspectedFile:
    """One repository file that was actually read for analysis."""

    path: str
    size_bytes: int
    line_count: int


@dataclass(frozen=True)
class SkippedSummary:
    """Exact count, plus a bounded sample, of files left out for one reason."""

    reason: str
    count: int
    recorded_paths: Tuple[str, ...]
    truncated: bool


@dataclass(frozen=True)
class RepositoryEvidence:
    """Deterministic record of one bounded, read-only repository read."""

    repository_name: str
    repository_path: str
    inspected_files: Tuple[InspectedFile, ...]
    inspected_file_count: int
    inspected_truncated: bool
    skipped: Tuple[SkippedSummary, ...]
    skipped_file_count: int
    ignored_directories: Tuple[Tuple[str, int], ...]
    total_bytes_read: int
    limits: RepositoryLimits
    safety_restrictions: Tuple[str, ...]

    @property
    def inspected_paths(self) -> Tuple[str, ...]:
        """Repository-relative paths of the recorded inspected files."""

        return tuple(entry.path for entry in self.inspected_files)


@dataclass(frozen=True)
class RepositoryLoad:
    """One completed read: the evidence, plus the bounded text it produced.

    ``files`` is the only way content leaves the reader. It is a materialised,
    sorted tuple of ``(repository-relative path, text)`` pairs, so a second
    analysis over the same repository sees the same bytes in the same order.
    """

    evidence: RepositoryEvidence
    files: Tuple[Tuple[str, str], ...]
