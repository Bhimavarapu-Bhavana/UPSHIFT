"""Secure, read-only repository input for UPSHIFT.

The controlled benchmark remains the deterministic demo input. This package is
the second input source: a real local repository the user explicitly points
UPSHIFT at, read through a boundary-validated, size-bounded, symlink-refusing
adapter.

Layering:

* :mod:`app.security.repository_input` decides *whether* a path may be read.
  It validates identity only and never opens a file.
* :mod:`app.repository.evidence` holds the immutable description of what a read
  actually covered.
* :mod:`app.repository.real_input` performs the read and returns a bounded
  snapshot for the existing analysis engines.
* :mod:`app.repository.snapshot` hands that bounded snapshot to the frozen
  Phase 4 engine, so the reference-discovery rules are reused rather than
  reimplemented.

The analyzed repository is never written. ``snapshot`` copies the bounded read
into a private scratch directory of UPSHIFT's own and removes it afterwards, so
the frozen analyzer can be used verbatim.

Nothing in this package executes, imports, writes, fetches, or restores. It
cannot migrate a repository and cannot make one safe on its own; it produces
evidence, and the existing Phase 4-9 engines decide from it.
"""

from __future__ import annotations

from app.repository.evidence import (
    DEFAULT_LIMITS,
    InspectedFile,
    RepositoryEvidence,
    RepositoryLimits,
    RepositoryLoad,
    SkippedSummary,
)
from app.repository.real_input import (
    NoControlledRollbackProvider,
    RealRepositoryInput,
    RepositoryInput,
    RepositoryReadOnlyError,
)
from app.repository.snapshot import bounded_snapshot

__all__ = [
    "DEFAULT_LIMITS",
    "InspectedFile",
    "NoControlledRollbackProvider",
    "RealRepositoryInput",
    "RepositoryEvidence",
    "RepositoryInput",
    "RepositoryLimits",
    "RepositoryLoad",
    "RepositoryReadOnlyError",
    "SkippedSummary",
    "bounded_snapshot",
]
