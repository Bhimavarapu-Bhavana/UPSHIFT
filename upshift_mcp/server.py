"""STDIO-only MCP server exposing read-only UPSHIFT migration evidence.

The server is built on the official MCP Python SDK (``mcp==2.2.0``) through
``from mcp.server.mcpserver import MCPServer``. The local UPSHIFT package lives
under ``upshift_mcp`` so that it does not shadow that SDK.

Scope boundary:

* Local STDIO transport only. No SSE, no Streamable HTTP, no HTTP listener, no
  public server, and no remote or cloud deployment.
* Exactly one controlled read-only tool, ``get_migration_context``, which serves
  two modes: the controlled benchmark, and an operator-approved real repository.
* No migration execution, no shell, subprocess, or dynamic Python execution, no
  caller-supplied filesystem path, no credential or ``.env`` access, no network
  request, and no Git state modification.
* Real-repository evidence is available only for a directory an operator listed
  in ``.upshift/mcp-repository.json``; a caller selects among approved roots by
  name and can never introduce or widen one.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.security.repository_input import RepositoryInputError

from .context import (
    DEFAULT_CANDIDATE_ID,
    PROHIBITED_CAPABILITIES,
    UnknownCandidateError,
    build_migration_context,
    build_real_repository_context,
    known_candidate_ids,
)
from .repository_boundary import mcp_repository_names

__all__ = [
    "DEFAULT_CANDIDATE_ID",
    "MIGRATION_CONTEXT_TOOL",
    "PROHIBITED_CAPABILITIES",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TRANSPORT",
    "build_server",
    "get_migration_context",
    "get_real_repository_context",
    "known_candidate_ids",
    "main",
    "run_mcp_server",
    "server",
]

SERVER_NAME = "upshift-migration-evidence"
SERVER_VERSION = "0.1.0"

#: The only transport this foundation supports.
TRANSPORT = "stdio"

MIGRATION_CONTEXT_TOOL = "get_migration_context"

SERVER_INSTRUCTIONS = (
    "UPSHIFT read-only migration evidence over local STDIO. Call "
    f"{MIGRATION_CONTEXT_TOOL} with a known candidate_id for controlled "
    "benchmark evidence, or with a repository name plus a migration "
    "declaration for real-repository evidence. The repository name must be one "
    "an operator approved; no path is accepted. This server never executes a "
    "migration, never modifies a repository, and never reports a real "
    "repository as accepted."
)

_KNOWN_CANDIDATE_IDS = ", ".join(known_candidate_ids())


def _approved_repository_names() -> str:
    """Names an operator currently approved, or a short closed-boundary note."""

    try:
        names = mcp_repository_names()
    except Exception:  # noqa: BLE001 - never let a config problem hide discovery
        return "none (no operator-approved repository is configured)"
    if not names:
        return "none (no operator-approved repository is configured)"
    return ", ".join(sorted(names))


def build_server() -> MCPServer:
    """Create the UPSHIFT MCP server and register its read-only tools."""

    instance = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )

    @instance.tool(
        name=MIGRATION_CONTEXT_TOOL,
        title="Get UPSHIFT migration context",
        description=(
            "Return read-only UPSHIFT migration evidence. Pass a known "
            "candidate_id for the controlled benchmark: migration name, "
            "impacted files, direct and target references, risk level and "
            "score, verification status and case counts, decision state, and "
            f"the supporting evidence. Known candidate_id values: "
            f"{_KNOWN_CANDIDATE_IDS}. To analyse a real repository instead, "
            "pass an approved repository name together with a migration "
            "declaration (name, old_api, target_api, and optionally symbols); "
            "the result is then mode=real_repository and adds the inspected "
            "files, skipped files, and read limits actually applied. Approved "
            f"repository names: {_approved_repository_names()}. The tool is "
            "read-only, never executes a migration, and accepts no filesystem "
            "path."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _get_migration_context(
        candidate_id: str = DEFAULT_CANDIDATE_ID,
        repository: Optional[str] = None,
        migration: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if repository is None and migration is None:
            try:
                return get_migration_context(candidate_id)
            except UnknownCandidateError as error:
                raise ToolError(str(error)) from None

        if repository is None:
            raise ToolError(
                "real-repository mode requires a repository name: pass the name "
                "of an operator-approved repository, or omit both repository and "
                "migration to read the controlled benchmark"
            )

        try:
            return get_real_repository_context(repository, migration)
        except (UnknownCandidateError, RepositoryInputError) as error:
            raise ToolError(str(error)) from None

    return instance


#: Module-level server instance. Importing this module does not start a server.
server = build_server()


def get_migration_context(
    candidate_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the read-only migration context for an allowlisted candidate.

    This is the controlled read-only MCP tool implementation. It performs no
    migration, modifies nothing, and accepts no filesystem path or command.

    Raises:
        UnknownCandidateError: If ``candidate_id`` is not allowlisted. The
            registered tool translates this into a deliberate ``ToolError`` so
            that a rejected value is a controlled refusal rather than a crash.
    """

    return build_migration_context(candidate_id)


def get_real_repository_context(
    repository: Optional[str] = None,
    migration: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return read-only migration context for an operator-approved repository.

    This performs no migration, modifies nothing, and accepts no filesystem
    path: ``repository`` is the *name* of a directory an operator listed in
    ``.upshift/mcp-repository.json``.

    Raises:
        RepositoryInputError: If no boundary is approved, the name is not an
            approved name, or the declaration is malformed. The registered tool
            translates this into a deliberate ``ToolError`` so a rejected
            request is a controlled refusal rather than a crash.
    """

    return build_real_repository_context(repository, migration)


def run_mcp_server() -> None:
    """Run the server on local STDIO. The only supported transport."""

    server.run(transport=TRANSPORT)


def main() -> None:
    """Console entry point for the UPSHIFT MCP server."""

    run_mcp_server()
