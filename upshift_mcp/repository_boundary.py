"""Operator-approved repository boundary for the UPSHIFT MCP server.

This module answers exactly one question: *which local directories, if any, did
the operator allow the MCP server to read?* The answer comes from a
project-local configuration file the operator edits, and it is validated by the
**existing** boundary machinery in :mod:`app.security.repository_input`. No
second boundary, and no second repository reader, is introduced here.

Why a configuration file rather than an environment variable
-------------------------------------------------------------
The MCP package deliberately declares no environment-variable access at all
(``os.environ`` and ``getenv`` are forbidden in ``upshift_mcp`` by
``tests/test_bob_mcp_integration.py``). Keeping that ban intact is worth more
than the convenience of ``export``, so the approved roots are read from a
reviewed file instead. The trade-off is deliberate and is recorded here: an
operator approval is auditable in a diff rather than hidden in a shell profile.

Deliberate non-capabilities
---------------------------
* No caller-supplied path is ever turned into a root. Only the operator's file
  names the boundary; an MCP caller can *select* among approved roots but can
  never introduce one.
* No environment variable, credential, secret, or ``.env`` file is read.
* No shell, subprocess, Git, or network operation is performed, and nothing is
  written.
* A missing configuration file yields a **closed** boundary, so the server stays
  in controlled-benchmark mode and real-repository evidence is unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Tuple

from app.security.repository_input import (
    RepositoryBoundary,
    RepositoryBoundaryError,
    build_repository_boundary,
)

__all__ = [
    "MCP_REPOSITORY_CONFIG_PATH",
    "MCP_REPOSITORY_POLICY",
    "McpBoundaryConfigurationError",
    "mcp_repository_boundary",
    "mcp_repository_names",
]

#: Project-local operator configuration. Relative to the repository root so the
#: file stays portable: no machine-specific absolute path is committed.
MCP_REPOSITORY_CONFIG_PATH = Path(__file__).resolve().parents[1] / ".upshift" / "mcp-repository.json"

#: Human-readable statement of the rule, safe to place in a tool result.
MCP_REPOSITORY_POLICY = (
    "The MCP server reads a real repository only when an operator listed its "
    "directory in .upshift/mcp-repository.json. A caller chooses among the "
    "approved roots by name and can never supply a path of its own; the "
    "boundary is checked again on the resolved path, so traversal and link "
    "escapes are refused before any file is read."
)

_CONFIG_KEY = "allowed_repository_roots"


class McpBoundaryConfigurationError(RepositoryBoundaryError):
    """Raised when the operator boundary file exists but cannot be trusted.

    This fails closed *loudly*: a malformed or unreadable approval is never
    treated as an empty approval, because silently degrading to benchmark mode
    would hide a configuration mistake from the operator who made it.
    """


def _read_config() -> Any:
    """Return the parsed configuration, or ``None`` when no file is present."""

    if not MCP_REPOSITORY_CONFIG_PATH.is_file():
        return None
    try:
        return json.loads(MCP_REPOSITORY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise McpBoundaryConfigurationError(
            f"{MCP_REPOSITORY_CONFIG_PATH.name} could not be read as JSON"
        ) from error


def _config_roots(config: Any) -> Tuple[str, ...]:
    """Validate the approval file and return absolute root strings.

    Relative entries are resolved against the project root so the approval can
    be committed and still mean the same thing on another machine. The strict
    "must be absolute" rule in :mod:`app.security.repository_input` is
    deliberately not weakened: this function satisfies it first, and that
    module still decides whether each result is an existing directory.
    """

    if not isinstance(config, dict):
        raise McpBoundaryConfigurationError(
            f"{MCP_REPOSITORY_CONFIG_PATH.name} must contain a JSON object"
        )

    unexpected = sorted(set(config) - {_CONFIG_KEY})
    if unexpected:
        raise McpBoundaryConfigurationError(
            f"{MCP_REPOSITORY_CONFIG_PATH.name} accepts only: {_CONFIG_KEY}"
        )

    values = config.get(_CONFIG_KEY)
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise McpBoundaryConfigurationError(
            f"{_CONFIG_KEY} must be a list of directory paths"
        )

    project_root = MCP_REPOSITORY_CONFIG_PATH.parents[1]
    roots = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise McpBoundaryConfigurationError(
                f"every {_CONFIG_KEY} entry must be a non-empty string"
            )
        candidate = Path(value.strip())
        if not candidate.is_absolute():
            candidate = project_root / candidate
        roots.append(str(candidate))

    return tuple(roots)


def mcp_repository_boundary() -> RepositoryBoundary:
    """Return the operator-approved boundary for this MCP process.

    Returns:
        The existing :class:`~app.security.repository_input.RepositoryBoundary`
        built from the operator's approved roots. An unconfigured server
        returns a **closed** boundary, which refuses every real-repository
        request.

    Raises:
        McpBoundaryConfigurationError: If the approval file exists but is
            malformed, or names a root that is not an existing directory.
    """

    config = _read_config()
    if config is None:
        return build_repository_boundary(None)
    return build_repository_boundary(_config_roots(config))


def mcp_repository_names() -> Tuple[str, ...]:
    """Return the names a caller may use to select an approved root.

    A name is the final path segment of an approved root. It is an identifier,
    not a path, so selecting with one cannot escape the boundary: the selected
    root is passed back through the same containment check as any other path.
    """

    boundary = mcp_repository_boundary()
    return tuple(root.name for root in boundary.roots)


def select_approved_root(boundary: RepositoryBoundary, name: Optional[str]) -> Path:
    """Resolve a caller-supplied *name* to one operator-approved root.

    Args:
        boundary: The boundary built by :func:`mcp_repository_boundary`.
        name: The approved root's name, or ``None`` when the operator approved
            exactly one root and the caller did not need to disambiguate.

    Returns:
        The resolved, contained root directory.

    Raises:
        RepositoryBoundaryError: If no root is approved, if the caller omitted
            the name while several roots are approved, or if the name is not
            one of the approved names. Refusal messages list approved names and
            never echo a caller-supplied value.
    """

    if not boundary.is_configured:
        raise RepositoryBoundaryError(
            "no repository boundary is configured, so real-repository evidence "
            "is unavailable; an operator must add a root to "
            f"{MCP_REPOSITORY_CONFIG_PATH.name} first"
        )

    names = tuple(root.name for root in boundary.roots)
    if name is None:
        if len(boundary.roots) != 1:
            raise RepositoryBoundaryError(
                "several approved repositories exist, so one must be named; "
                "approved repositories: " + ", ".join(sorted(names))
            )
        return boundary.roots[0]

    if not isinstance(name, str) or name not in names:
        raise RepositoryBoundaryError(
            "unknown approved repository; approved repositories: "
            + ", ".join(sorted(names))
        )
    for root in boundary.roots:
        if root.name == name:
            return root
    raise RepositoryBoundaryError("unknown approved repository")
