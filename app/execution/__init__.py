"""Bounded migration execution for UPSHIFT.

UPSHIFT reads real repositories; this package is where it is allowed to *change*
one, and it is deliberately the smallest thing that can do that safely.

The capability is not "run code". It is one structured operation:

    replace N occurrences of an exact expected string, in one known file,
    with an exact intended string, and assert the count.

Everything a general execution facility would need is absent by construction:

* no command, no argument vector, no shell, no subprocess, no interpreter;
* no ``eval``, no ``exec``, no ``__import__``, no dynamic import of any kind;
* no absolute path, no ``..`` segment, no drive letter, no UNC path;
* no file outside the operator-approved boundary;
* no file the bounded reader did not already admit, which excludes links,
  excluded directories, sensitive names, and anything over the read budgets;
* no partial application: the whole plan is validated before any byte is
  written, so a rejected plan leaves the repository exactly as it was.

The operation is *textual and exact*. It cannot express a transformation, only a
replacement the operator has already decided on, which is what makes
"expected old content" and "expected occurrence count" meaningful: they are
preconditions, checked against the real filesystem at execution time.

A migration is never accepted because it executed. Execution produces
filesystem evidence; the existing Phase 6 verifier and Phase 7 decision engine
still decide, and the existing Phase 9 recovery engine still recovers.
"""

from __future__ import annotations

__all__ = [
    "EXECUTION_LIMITS",
    "FileOperation",
    "FileOperationError",
    "MigrationExecutor",
    "OPERATION_REPLACE_EXACT",
    "SUPPORTED_OPERATIONS",
    "require_file_operations",
    "require_file_operation",
]
