"""Bridge a bounded repository read to the frozen Phase 4 impact engine.

The existing :class:`app.core.impact_analyzer.ImpactAnalyzer` is a protected
Phase 4 file: this project deliberately freezes it, and
``tests/test_recovery_engine.py`` asserts its digest. That is a good constraint,
because it means the reference-discovery rules cannot drift between the
controlled benchmark and real-repository mode.

The analyzer discovers references by walking a directory, so feeding it an
in-memory snapshot would mean either changing a frozen file or reimplementing
its rules, which would let the two modes drift apart. This module takes the third
route: it materialises the *already-bounded* read into a private scratch
directory and points the unchanged analyzer at that.

What this means for the repository being analyzed
------------------------------------------------

* The analyzed repository is **never written**. It was opened read-only and
  every byte came back through
  :meth:`app.repository.real_input.RealRepositoryInput.load`.
* Only the files the reader admitted, with only the content the reader read, are
  copied. Nothing is discovered again, so the analyzer cannot reach a file the
  reader refused, and it cannot exceed the read budgets.
* The scratch directory belongs to UPSHIFT, not to the user, and is removed when
  the analysis ends, including when the analysis fails. This is the same
  pattern the controlled benchmark already uses for its rollback workspace.
* A snapshot label is re-checked for containment before it is written, so a
  malformed label cannot escape the scratch directory.

This module performs no analysis. It moves bytes the reader already decided
about, and it deletes them afterwards.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence, Tuple

__all__ = ["SNAPSHOT_PREFIX", "bounded_snapshot"]


#: Prefix for the private scratch directory, so an abandoned snapshot is
#: identifiable as UPSHIFT's own and not as part of a user's repository.
SNAPSHOT_PREFIX = "upshift-snapshot-"


@contextmanager
def bounded_snapshot(
    files: Sequence[Tuple[str, str]],
    *,
    prefix: str = SNAPSHOT_PREFIX,
) -> Iterator[Path]:
    """Materialise a bounded read into a private scratch directory.

    Args:
        files: The ``(repository-relative label, text)`` pairs produced by a
            bounded repository read.
        prefix: Directory-name prefix for the scratch directory.

    Yields:
        The scratch directory to hand to the existing impact analyzer.

    Raises:
        ValueError: If a label is absolute or would escape the scratch
            directory. The reader already produces contained labels; this is a
            second, independent check before anything is written.
    """

    # Resolved once, so the containment check below compares like with like. A
    # Windows temporary directory can be reached by a short name, which would
    # otherwise make every label look like an escape.
    root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    try:
        for label, text in files:
            destination = _destination(root, label)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # ``newline=""`` is required, not cosmetic. Without it the platform
            # translates every "\n" it writes into ``os.linesep``, so a repository
            # file that already uses CRLF endings is stored as CR CR LF. The
            # analyzer's universal-newline read then sees a blank line at every
            # original line break, which shifts every reported line number after
            # the first and inflates the recorded line count. Writing verbatim
            # keeps the snapshot byte-identical to what the reader read, so the
            # evidence a caller sees describes the repository and not this copy.
            with destination.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _destination(root: Path, label: str) -> Path:
    """Resolve one snapshot label, refusing anything that leaves the scratch root."""

    relative = Path(label)
    if relative.is_absolute() or not relative.parts:
        raise ValueError("a snapshot label must be a relative path inside the scratch root")

    destination = (root / relative).resolve()
    if destination != root and root not in destination.parents:
        raise ValueError("a snapshot label must not escape the scratch root")
    return destination
