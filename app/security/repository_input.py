"""Repository-boundary and migration-declaration validation for UPSHIFT.

This module is the *only* place a caller-supplied filesystem path is inspected.
It is the security boundary between a caller and the repository reader in
:mod:`app.repository`.

What it enforces
----------------

* A repository path is accepted only when it is an **absolute** local path that
  resolves inside a boundary root the operator explicitly allowed. There is no
  default, no discovery, and no search: when no boundary root is configured,
  every path is refused.
* The check is performed on the *resolved* path, so ``..`` segments, symbolic
  links, and Windows junctions cannot carry a path outside the boundary. A
  traversal attempt fails the containment test exactly like any other outside
  path, and the refusal message never echoes the rejected value.
* Only the allowed roots come from the operator (the dashboard entry point).
  A caller can narrow the boundary but can never widen it.
* The migration declaration is normalized into identifier-like symbols only. A
  declaration cannot smuggle a path, a command, or a filesystem API through the
  symbol fields, because symbols are matched against a strict identifier
  grammar before they reach the impact analyzer.
* A declaration never invents a search vocabulary of its own. When a caller
  omits ``old_symbols`` or ``target_symbols`` they are left empty here, and the
  existing Phase 4 ``MigrationDescription.from_metadata`` derives them from the
  entry points, exactly as it does for the controlled benchmark. Both modes
  therefore share one vocabulary rule, and the frozen Phase 4 engine stays the
  single owner of it.

Deliberate non-capabilities
---------------------------

* No shell, subprocess, or Python execution, and no dynamic import.
* No file is opened, read, written, or deleted here. The module resolves
  identities with :mod:`pathlib` only; content reading lives in
  :mod:`app.repository`, behind this boundary.
* No network access, no Git invocation, and no credential, key, or environment
  value is read. Nothing is echoed back: a refusal message never contains the
  path or declaration that was rejected.
* Nothing is written, so no repository file and no Git state is modified.

This module validates identity and shape only. It performs no analysis and
holds no migration state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BOUNDARY_POLICY",
    "MAX_DECLARATION_TEXT",
    "MAX_SYMBOL_LENGTH",
    "MODE_DEMO",
    "MODE_REAL_REPOSITORY",
    "MigrationDeclaration",
    "RepositoryBoundary",
    "RepositoryBoundaryError",
    "RepositoryDeclarationError",
    "RepositoryInputError",
    "build_repository_boundary",
    "require_migration_declaration",
    "require_repository_path",
]

#: The two analysis modes the dashboard can report.
MODE_DEMO = "demo"
MODE_REAL_REPOSITORY = "real_repository"

#: Human-readable statement of the boundary rule, safe to display.
BOUNDARY_POLICY = (
    "A repository path is analysed only when it resolves inside a directory the "
    "operator explicitly allowed. Paths outside that boundary, and traversal or "
    "link escapes, are refused before any file is read."
)

#: Longest accepted path string. A path is a bounded input, not a payload.
MAX_PATH_LENGTH = 4096

#: Longest accepted free-text field in a migration declaration.
MAX_DECLARATION_TEXT = 200

#: Longest accepted single symbol.
MAX_SYMBOL_LENGTH = 128

#: A symbol is a dotted identifier chain. Nothing else is accepted, so a symbol
#: can never be a path fragment, a shell word, or a newline-bearing string.
_SYMBOL = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,127}(?:\.[A-Za-z_][A-Za-z0-9_]{0,127})*\Z")

_CONTROL_CHARACTERS = frozenset(chr(code) for code in list(range(0, 32)) + [127])

_MIGRATION_FIELDS = (
    "name",
    "old_api",
    "target_api",
    "old_symbols",
    "target_symbols",
    "renamed_symbols",
)


class RepositoryInputError(ValueError):
    """Base class for every repository-input refusal.

    Messages are written for a human and never contain the value that was
    refused, so a rejected path or declaration cannot be probed.
    """


class RepositoryBoundaryError(RepositoryInputError):
    """Raised when a repository path is not an allowed local directory."""


class RepositoryDeclarationError(RepositoryInputError):
    """Raised when a migration declaration is absent or malformed."""


# --------------------------------------------------------------------------
# Declaration shape
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationDeclaration:
    """A validated, identifier-only description of one migration.

    The declaration is *data*. It names what is being migrated so the existing
    impact analyzer knows which symbols to search for. It cannot name a file to
    read, a module to import, or a command to run: there is no such field, and
    the symbol fields only accept a dotted-identifier grammar.
    """

    name: str
    old_api: str
    target_api: str
    old_symbols: Tuple[str, ...]
    target_symbols: Tuple[str, ...]
    renamed_symbols: Tuple[Tuple[str, str], ...] = ()

    @property
    def symbols(self) -> Tuple[str, ...]:
        """Every declared symbol, old and target, in deterministic order."""

        return tuple(sorted(set(self.old_symbols) | set(self.target_symbols)))


def _declaration_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RepositoryDeclarationError(f"{field_name} must be a string")
    # Control characters are checked on the raw value, before trimming. Checking
    # afterwards would let a value smuggle a trailing newline past the check and
    # then have it quietly removed.
    if any(character in _CONTROL_CHARACTERS for character in value):
        raise RepositoryDeclarationError(f"{field_name} must not contain control characters")
    text = value.strip()
    if not text:
        raise RepositoryDeclarationError(f"{field_name} must not be empty")
    if len(text) > MAX_DECLARATION_TEXT:
        raise RepositoryDeclarationError(
            f"{field_name} must be at most {MAX_DECLARATION_TEXT} characters"
        )
    return text


def _declaration_symbol(value: Any, field_name: str) -> str:
    text = _declaration_text(value, field_name)
    if len(text) > MAX_SYMBOL_LENGTH or not _SYMBOL.match(text):
        raise RepositoryDeclarationError(
            f"{field_name} must be a dotted identifier such as Module.symbol"
        )
    return text


def _declaration_symbols(value: Any, field_name: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise RepositoryDeclarationError(
            f"{field_name} must be a list of identifiers, not a single string"
        )
    if not isinstance(value, Sequence):
        raise RepositoryDeclarationError(f"{field_name} must be a list of identifiers")
    symbols = [
        _declaration_symbol(item, f"{field_name} entry")
        for item in value
    ]
    return tuple(sorted(set(symbols)))


def _declaration_renames(value: Any) -> Tuple[Tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RepositoryDeclarationError(
            "renamed_symbols must be a list of [old, target] identifier pairs"
        )
    pairs = []
    for item in value:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence):
            raise RepositoryDeclarationError(
                "renamed_symbols must be a list of [old, target] identifier pairs"
            )
        if len(item) != 2:
            raise RepositoryDeclarationError(
                "each renamed_symbols entry must hold exactly two identifiers"
            )
        pairs.append(
            (
                _declaration_symbol(item[0], "renamed_symbols old name"),
                _declaration_symbol(item[1], "renamed_symbols target name"),
            )
        )
    return tuple(sorted(set(pairs)))


def require_migration_declaration(value: Any) -> MigrationDeclaration:
    """Return a validated migration declaration or refuse the request.

    Args:
        value: The raw ``migration`` object supplied by the caller.

    Returns:
        A :class:`MigrationDeclaration` whose symbols are all identifier-like.

    Raises:
        RepositoryDeclarationError: If the declaration is absent, is not an
            object, carries an unexpected field, is missing a required field,
            or contains anything that is not a dotted identifier.
    """

    if not isinstance(value, Mapping):
        raise RepositoryDeclarationError("migration must be an object")

    unexpected = sorted(set(value) - set(_MIGRATION_FIELDS))
    if unexpected:
        raise RepositoryDeclarationError(
            "migration accepts only: " + ", ".join(_MIGRATION_FIELDS)
        )

    missing = sorted(
        field
        for field in ("name", "old_api", "target_api")
        if field not in value or value[field] is None
    )
    if missing:
        raise RepositoryDeclarationError(
            "migration is missing: " + ", ".join(missing)
        )

    name = _declaration_text(value["name"], "migration name")
    old_api = _declaration_text(value["old_api"], "old_api")
    target_api = _declaration_text(value["target_api"], "target_api")

    old_symbols = _declaration_symbols(value.get("old_symbols"), "old_symbols")
    target_symbols = _declaration_symbols(value.get("target_symbols"), "target_symbols")

    # Symbols a caller omitted are left empty on purpose. The existing Phase 4
    # engine owns the entry-point vocabulary rule, so a declaration that omits
    # them is completed downstream by the same code path the controlled
    # benchmark uses, and the two modes cannot drift apart.
    return MigrationDeclaration(
        name=name,
        old_api=old_api,
        target_api=target_api,
        old_symbols=old_symbols,
        target_symbols=target_symbols,
        renamed_symbols=_declaration_renames(value.get("renamed_symbols")),
    )


# --------------------------------------------------------------------------
# Repository boundary
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RepositoryBoundary:
    """The set of directories inside which a repository may be read.

    An empty ``roots`` tuple is a valid, *closed* boundary: it means no
    directory is allowed. That is the default, so real-repository analysis is
    unavailable until an operator explicitly allows a root.
    """

    roots: Tuple[Path, ...] = ()

    @property
    def is_configured(self) -> bool:
        """True when at least one boundary root was explicitly allowed."""

        return bool(self.roots)

    def resolve(self, value: Any) -> Path:
        """Return the resolved repository path or refuse the request.

        Args:
            value: The raw path string supplied by the caller.

        Returns:
            The fully resolved absolute :class:`~pathlib.Path` inside a boundary
            root.

        Raises:
            RepositoryBoundaryError: If no boundary is configured, the value is
                not a plausible absolute local path, the target does not exist or
                is not a directory, or the resolved path falls outside every
                allowed root. The message never echoes the supplied value.
        """

        if not self.is_configured:
            raise RepositoryBoundaryError(
                "no repository boundary is configured, so real-repository "
                "analysis is unavailable; start the dashboard with an explicit "
                "allowed repository root"
            )

        raw = _boundary_path_text(value)
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise RepositoryBoundaryError(
                "repository_path must be an absolute local filesystem path"
            )

        try:
            resolved = candidate.resolve()
        except (OSError, ValueError) as error:
            raise RepositoryBoundaryError(
                "repository_path could not be resolved to a local filesystem path"
            ) from error

        if not resolved.is_dir():
            raise RepositoryBoundaryError(
                "repository_path must be an existing directory"
            )

        if not any(self._contains(root, resolved) for root in self.roots):
            raise RepositoryBoundaryError(
                "repository_path is outside the allowed repository boundary"
            )

        return resolved

    @staticmethod
    def _contains(root: Path, resolved: Path) -> bool:
        """Containment test on already-resolved paths."""

        try:
            resolved.relative_to(root)
        except ValueError:
            return False
        return True

    def describe(self) -> Tuple[str, ...]:
        """Boundary description that never discloses a filesystem path."""

        if not self.is_configured:
            return ("no repository boundary is configured",)
        return (
            f"{len(self.roots)} allowed repository "
            f"{'root' if len(self.roots) == 1 else 'roots'}",
        )


def _boundary_path_text(value: Any) -> str:
    if not isinstance(value, str):
        raise RepositoryBoundaryError("repository_path must be a string")

    # As with a declaration, control characters are refused on the raw value
    # rather than after trimming, so a value cannot hide one at the end.
    if any(character in _CONTROL_CHARACTERS for character in value):
        raise RepositoryBoundaryError(
            "repository_path must not contain control characters"
        )

    text = value.strip()
    if not text:
        raise RepositoryBoundaryError("repository_path must not be empty")
    if len(text) > MAX_PATH_LENGTH:
        raise RepositoryBoundaryError("repository_path is too long")
    if "://" in text or text[:5].lower() == "file:":
        raise RepositoryBoundaryError(
            "repository_path must be a local filesystem path, not a URL"
        )
    return text


def _boundary_root_text(value: Any, index: int) -> str:
    """Validate one operator-supplied boundary root.

    A root is operator configuration rather than caller input, so it is
    accepted as given apart from the checks that would make the boundary
    meaningless: it must be a plausible absolute local directory path.
    """

    if not isinstance(value, str):
        raise RepositoryBoundaryError(f"allowed repository root {index + 1} must be a string")

    if any(character in _CONTROL_CHARACTERS for character in value):
        raise RepositoryBoundaryError(
            f"allowed repository root {index + 1} must not contain control characters"
        )

    text = value.strip()
    if not text:
        raise RepositoryBoundaryError(f"allowed repository root {index + 1} must not be empty")
    if len(text) > MAX_PATH_LENGTH:
        raise RepositoryBoundaryError(f"allowed repository root {index + 1} is too long")
    if "://" in text or text[:5].lower() == "file:":
        raise RepositoryBoundaryError(
            f"allowed repository root {index + 1} must be a local filesystem path"
        )
    if not Path(text).is_absolute():
        raise RepositoryBoundaryError(
            f"allowed repository root {index + 1} must be an absolute local path"
        )
    return text


def build_repository_boundary(roots: Optional[Sequence[Any]] = None) -> RepositoryBoundary:
    """Build a boundary from operator-supplied roots.

    Args:
        roots: Absolute local directory paths the operator explicitly allows.
            ``None`` or an empty sequence produces a closed boundary.

    Returns:
        A :class:`RepositoryBoundary` over the resolved, de-duplicated roots in
        deterministic order.

    Raises:
        RepositoryBoundaryError: If any configured root is not an existing
            absolute local directory. The server therefore fails closed at
            start-up rather than silently analysing something unexpected.
    """

    if roots is None:
        return RepositoryBoundary()
    if isinstance(roots, (str, bytes)) or not isinstance(roots, Sequence):
        raise RepositoryBoundaryError("allowed repository roots must be a list of paths")

    resolved: list[Path] = []
    for index, value in enumerate(roots):
        text = _boundary_root_text(value, index)
        try:
            path = Path(text).resolve()
        except (OSError, ValueError) as error:
            raise RepositoryBoundaryError(
                f"allowed repository root {index + 1} could not be resolved"
            ) from error
        if not path.is_dir():
            raise RepositoryBoundaryError(
                f"allowed repository root {index + 1} is not an existing directory"
            )
        if path not in resolved:
            resolved.append(path)

    return RepositoryBoundary(roots=tuple(sorted(resolved)))


def require_repository_path(boundary: RepositoryBoundary, value: Any) -> Path:
    """Validate ``value`` against ``boundary``.

    Thin wrapper over :meth:`RepositoryBoundary.resolve` so callers read as a
    validation step rather than as a method call on a boundary object.
    """

    if not isinstance(boundary, RepositoryBoundary):
        raise RepositoryBoundaryError("a repository boundary is required")
    return boundary.resolve(value)
