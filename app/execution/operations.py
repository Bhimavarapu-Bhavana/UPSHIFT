"""The one structured operation UPSHIFT will execute, and its validation.

An operation is *data*. It names a file relative to an approved repository root,
the exact text it expects to find, the exact text it intends to put there, and
how many times it expects to find it. Those four things plus the operation name
are the entire vocabulary, and every field is bounded before it is used.

What is deliberately *not* representable
---------------------------------------
A value that would let a caller execute code, reach outside the boundary, or
change a file nobody reviewed simply does not parse. There is no field for it:

* no ``command``, ``args``, ``shell``, ``script``, or ``code`` field;
* no ``module`` or ``import`` field, so nothing can be loaded and run;
* no absolute path, drive letter, UNC prefix, or ``..`` segment, because
  :func:`require_file_operation` rejects them outright;
* no "create", "delete", "move", "chmod", or "rename" operation, so the only
  structural change a plan can express is rewriting text inside a file the
  bounded reader already admitted.

Validation happens in two independent places. This module rejects malformed
data before the executor ever sees it, and
:class:`app.execution.migration_executor.MigrationExecutor` re-checks the path
against the live boundary and the current file content before writing. Neither
check trusts the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple

__all__ = [
    "EXECUTION_LIMITS",
    "FileOperation",
    "FileOperationError",
    "OPERATION_REPLACE_EXACT",
    "SUPPORTED_OPERATIONS",
    "require_file_operations",
    "require_file_operation",
]

#: The only operation that exists. Naming it explicitly means an unknown
#: operation is refused as *unsupported* rather than ignored, so a typo can
#: never silently become a no-op that reports success.
OPERATION_REPLACE_EXACT = "replace_exact"

#: Every operation name the executor will accept.
SUPPORTED_OPERATIONS: Tuple[str, ...] = (OPERATION_REPLACE_EXACT,)

#: Bounds applied to a plan. They exist so a plan cannot be used to exhaust
#: memory or to turn one request into an unbounded amount of writing.
EXECUTION_LIMITS = {
    "max_operations_per_plan": 50,
    "max_operation_path_length": 512,
    "max_old_content_length": 65_536,
    "max_new_content_length": 65_536,
    "max_expected_occurrences": 1_000,
}

_OPERATION_FIELDS = frozenset(
    {"operation", "path", "old_content", "new_content", "expected_occurrences"}
)

_REQUIRED_FIELDS = frozenset({"operation", "path", "old_content", "new_content"})

_CONTROL_CHARACTERS = frozenset(chr(code) for code in list(range(0, 32)) + [127])

#: A repository-relative path: POSIX or Windows separators, no drive, no leading
#: separator, no ``.`` or ``..`` component, no empty component.
_RELATIVE_PATH = re.compile(r"\A[A-Za-z0-9_.\-/\\ ]+\Z")

_RESERVED_COMPONENTS = frozenset({"", ".", ".."})


class FileOperationError(ValueError):
    """Raised when an operation is malformed, unsupported, or unsafe.

    The message never echoes the supplied content or path. A refused operation
    is reported by its field name and the reason, which is enough to fix a plan
    and useless for probing the filesystem.
    """


@dataclass(frozen=True)
class FileOperation:
    """One validated, bounded, textual file change.

    Attributes:
        operation: Always :data:`OPERATION_REPLACE_EXACT`.
        path: Repository-relative path. Never absolute, never traversing.
        old_content: The exact text expected to be present.
        new_content: The exact text to put in its place.
        expected_occurrences: How many times ``old_content`` must occur. The
            executor refuses to write unless the count on disk matches exactly.
    """

    operation: str
    path: str
    old_content: str = field(repr=False)
    new_content: str = field(repr=False)
    expected_occurrences: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "old_content", _text(self.old_content, "old_content"))
        object.__setattr__(self, "new_content", _text(self.new_content, "new_content"))
        if self.old_content == self.new_content:
            raise FileOperationError(
                "new_content must differ from old_content, or the operation "
                "would change nothing while reporting a file change"
            )

    @property
    def label(self) -> str:
        """A safe, stable identifier for evidence and refusal messages.

        Only the repository-relative path is included. Content, which may be
        user data, never is.
        """

        return self.path

    def describe(self) -> Dict[str, Any]:
        """Return a JSON-serializable description carrying no content."""

        return {
            "operation": self.operation,
            "path": self.path,
            "expected_occurrences": self.expected_occurrences,
            "old_content_length": len(self.old_content),
            "new_content_length": len(self.new_content),
        }


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise FileOperationError(f"{field_name} must be a string")
    limit = EXECUTION_LIMITS[f"max_{field_name}_length"]
    if len(value) > limit:
        raise FileOperationError(
            f"{field_name} must be at most {limit} characters, got {len(value)}"
        )
    return value


def _path(value: Any) -> str:
    """Validate a repository-relative path, refusing anything that can escape.

    Absolute paths, drive letters, UNC prefixes, and ``..`` are rejected here,
    before the value is ever joined to a root. The executor repeats the check
    against the resolved real path; this one exists so a bad plan is refused
    even in a dry run that touches no filesystem.
    """

    if not isinstance(value, str):
        raise FileOperationError("path must be a string")
    if len(value) > EXECUTION_LIMITS["max_operation_path_length"]:
        raise FileOperationError(
            f"path must be at most {EXECUTION_LIMITS['max_operation_path_length']} characters"
        )
    text = value.strip()
    if not text:
        raise FileOperationError("path must not be empty")
    if "\x00" in text or any(character in _CONTROL_CHARACTERS for character in text):
        raise FileOperationError("path must not contain control characters")
    if not _RELATIVE_PATH.match(text):
        raise FileOperationError(
            "path must be a plain repository-relative path using only letters, "
            "digits, and the separators . _ - / \\"
        )
    if text.startswith(("/", "\\")) or ":" in text:
        raise FileOperationError(
            "path must be relative; absolute paths, drive letters, and UNC paths "
            "are refused"
        )

    parts = [part for part in re.split(r"[\\/]", text)]
    for part in parts:
        if part in _RESERVED_COMPONENTS:
            raise FileOperationError(
                "path must not contain an empty, '.' or '..' component"
            )
    if len(parts) < 2:
        raise FileOperationError(
            "path must name a file inside the repository, including its "
            "directory, such as 'pkg/service.py'"
        )
    return "/".join(parts)


def _occurrences(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FileOperationError("expected_occurrences must be an integer")
    if value < 1:
        raise FileOperationError("expected_occurrences must be at least 1")
    if value > EXECUTION_LIMITS["max_expected_occurrences"]:
        raise FileOperationError(
            "expected_occurrences must be at most "
            f"{EXECUTION_LIMITS['max_expected_occurrences']}"
        )
    return value


def require_file_operation(value: Any) -> FileOperation:
    """Validate one operation mapping into a :class:`FileOperation`.

    Args:
        value: A mapping with exactly the fields of one operation. Extra fields
            are refused rather than ignored, so a caller cannot smuggle a
            ``command`` key past a validator that only looks for known ones.

    Returns:
        The validated operation.

    Raises:
        FileOperationError: If the shape is wrong, the operation name is not
            supported, the path is not a safe repository-relative path, a
            content field is not a string, or the occurrence count is out of
            range.
    """

    if isinstance(value, FileOperation):
        return value
    if not isinstance(value, Mapping):
        raise FileOperationError("an operation must be a mapping")

    supplied = set(value)
    unexpected = sorted(supplied - _OPERATION_FIELDS)
    if unexpected:
        raise FileOperationError(
            "an operation does not accept these fields: " + ", ".join(unexpected)
        )
    missing = sorted(_REQUIRED_FIELDS - supplied)
    if missing:
        raise FileOperationError(
            "an operation is missing these fields: " + ", ".join(missing)
        )

    operation = value.get("operation")
    if operation not in SUPPORTED_OPERATIONS:
        raise FileOperationError(
            "unsupported operation; supported operations: "
            + ", ".join(SUPPORTED_OPERATIONS)
        )

    return FileOperation(
        operation=operation,
        path=_path(value.get("path")),
        old_content=value.get("old_content"),
        new_content=value.get("new_content"),
        expected_occurrences=_occurrences(value.get("expected_occurrences", 1)),
    )


def require_file_operations(value: Any) -> Tuple[FileOperation, ...]:
    """Validate a whole plan, refusing duplicate targets.

    A plan is validated in full before the executor writes anything, so a plan
    with one bad operation changes nothing at all. Two operations on the same
    file are refused because their order would decide the result, and an
    operation whose content depends on another's output is not a reviewable
    plan.
    """

    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise FileOperationError("a plan must be a list of operations")
    if not value:
        raise FileOperationError("a plan must contain at least one operation")
    if len(value) > EXECUTION_LIMITS["max_operations_per_plan"]:
        raise FileOperationError(
            f"a plan must contain at most {EXECUTION_LIMITS['max_operations_per_plan']} operations"
        )

    operations = tuple(require_file_operation(item) for item in value)
    seen: Dict[str, int] = {}
    for index, operation in enumerate(operations):
        if operation.path in seen:
            raise FileOperationError(
                f"two operations target the same file at positions {seen[operation.path]} "
                f"and {index}; one file may carry at most one operation per plan so the "
                "outcome does not depend on ordering"
            )
        seen[operation.path] = index
    return operations
