"""Read-only integration status for the UPSHIFT dashboard.

This module exists for one Phase 10.3 reason: a judge-facing demo must be able
to say exactly how far the IBM Bob integration has actually been verified, and
must never imply more than was proven.

It reports three *separate* facts, in increasing order of strength:

``configured``
    A project-scope MCP configuration exists, parses, and declares the UPSHIFT
    server. Established by reading the file, right now.

``local_mcp``
    The documented MCP surface - the tool name and the transport - is what the
    MCP package actually declares. Established by reading the package source.
    A test (``tests/test_bob_mcp_integration.py`` and
    ``tests/test_demo_workflow.py``) asserts that these declared values equal
    the tool list the live ``MCPServer`` registers, so the declaration cannot
    silently drift from reality.

``live_bob_runtime_verified``
    A real IBM Bob client completed a real session against this server. This
    module can never establish that fact, so it is always reported as
    unverified, with the reason attached. It is never inferred, simulated, or
    optimistically defaulted.

Security boundary enforced here:

* Read-only, and confined to the imports the Phase 10.1 dashboard guard
  allows: :mod:`json`, :mod:`pathlib`, :mod:`re`, and :mod:`typing`. It reads
  two fixed files and nothing else.
* The dashboard request path deliberately does **not** import ``upshift_mcp``.
  Importing the MCP package here would pull the MCP SDK into the HTTP layer
  and would require an event loop, which does not belong on a request path.
  The declaration is read as data instead.
* It adds no MCP tool. The documented surface stays exactly
  ``["get_migration_context"]``.
* It adds no HTTP route. Its result is attached to the existing
  ``GET /api/candidates`` self-description.
* The probe cannot become an execution vector: it inspects a fixed path, a
  fixed relative source path, and a fixed module constant. No caller input,
  no dynamic attribute access, and no code execution reaches it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

__all__ = [
    "BOB_CONFIG_RELATIVE_PATH",
    "BOB_RESPONSIBILITY",
    "EXPECTED_MCP_TOOLS",
    "EXPECTED_MCP_TRANSPORT",
    "MCP_SERVER_RELATIVE_PATH",
    "UPSHIFT_RESPONSIBILITY",
    "describe_integration",
]

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Project-scope IBM Bob MCP configuration shipped with this repository.
BOB_CONFIG_RELATIVE_PATH = ".bob/mcp.json"

#: The MCP server module whose declared surface this project reports.
MCP_SERVER_RELATIVE_PATH = "upshift_mcp/server.py"

#: The one and only MCP tool. Phase 10.3 must not add a second.
EXPECTED_MCP_TOOLS: Tuple[str, ...] = ("get_migration_context",)

#: The only transport the Phase 8 foundation supports.
EXPECTED_MCP_TRANSPORT = "stdio"

BOB_RESPONSIBILITY = (
    "IBM Bob is the agentic coding and orchestration layer. Bob plans and "
    "performs migration work. UPSHIFT never executes a migration and never "
    "acts on Bob's behalf."
)

UPSHIFT_RESPONSIBILITY = (
    "UPSHIFT is the evidence and safety layer: impact analysis, compatibility "
    "risk, independent behavioral verification, the migration decision, and "
    "controlled rollback with independent re-verification."
)

_LIVE_BOB_REASON = (
    "Not verified in this environment. A live IBM Bob client session against "
    "this server has not been observed, so no live Bob evidence is claimed."
)

#: Reads one simple module-level string constant, e.g. ``TRANSPORT = "stdio"``.
_CONSTANT_PATTERN = r'^{name}\s*(?::[^=]+)?=\s*["\']([^"\']+)["\']'


def _declared_constant(name: str) -> str:
    """Return a string constant declared at module level, or an empty string."""

    source = (_REPOSITORY_ROOT / MCP_SERVER_RELATIVE_PATH).read_text(encoding="utf-8")
    match = re.search(_CONSTANT_PATTERN.format(name=name), source, re.MULTILINE)
    return match.group(1) if match else ""


def _probe_configuration() -> Dict[str, Any]:
    """Report whether the project-scope Bob MCP config is present and valid."""

    path = _REPOSITORY_ROOT / BOB_CONFIG_RELATIVE_PATH
    if not path.is_file():
        return {
            "status": "missing",
            "evidence": f"{BOB_CONFIG_RELATIVE_PATH} is not present.",
            "verified_by": "local file read",
        }

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {
            "status": "unreadable",
            "evidence": (
                f"{BOB_CONFIG_RELATIVE_PATH} could not be parsed: "
                f"{type(error).__name__}."
            ),
            "verified_by": "local file read",
        }

    servers = document.get("mcpServers") if isinstance(document, dict) else None
    if not isinstance(servers, dict) or not servers:
        return {
            "status": "invalid",
            "evidence": f"{BOB_CONFIG_RELATIVE_PATH} declares no MCP servers.",
            "verified_by": "local file read",
        }

    names = sorted(servers)
    return {
        "status": "configured",
        "evidence": (
            f"{BOB_CONFIG_RELATIVE_PATH} declares {len(names)} MCP server(s): "
            + ", ".join(names)
            + "."
        ),
        "verified_by": "local file read",
        "declared_servers": names,
    }


def _probe_declared_surface() -> Dict[str, Any]:
    """Report the MCP surface the server package declares for itself."""

    try:
        tool = _declared_constant("MIGRATION_CONTEXT_TOOL")
        transport = _declared_constant("TRANSPORT")
    except OSError as error:
        return {
            "status": "unavailable",
            "evidence": (
                f"{MCP_SERVER_RELATIVE_PATH} could not be read: "
                f"{type(error).__name__}."
            ),
            "verified_by": "local file read",
        }

    if not tool or not transport:
        return {
            "status": "unavailable",
            "evidence": (
                f"{MCP_SERVER_RELATIVE_PATH} does not declare the documented "
                "tool and transport constants."
            ),
            "verified_by": "local file read",
        }

    matches = (tool,) == EXPECTED_MCP_TOOLS and transport == EXPECTED_MCP_TRANSPORT
    return {
        "status": "verified" if matches else "mismatch",
        "evidence": (
            f"{MCP_SERVER_RELATIVE_PATH} declares exactly one tool "
            f"({tool}) over {transport} transport, matching the documented "
            "read-only surface."
            if matches
            else (
                f"{MCP_SERVER_RELATIVE_PATH} declares {tool!r} over "
                f"{transport!r}, which does not match the documented surface."
            )
        ),
        "verified_by": (
            "local file read of the declared surface, asserted equal to the "
            "live registered tool list by the test suite"
        ),
        "transport": transport,
        "tools": [tool],
        "expected_tools": list(EXPECTED_MCP_TOOLS),
    }


def describe_integration() -> Dict[str, Any]:
    """Describe the IBM Bob / MCP integration status without overstating it.

    Returns:
        A JSON-serializable mapping with a ``levels`` list separating
        *configured*, *locally verified*, and *live Bob runtime verified*, plus
        the responsibility split between Bob and UPSHIFT.

    Raises:
        Nothing. Every probe degrades to a reported status instead of raising,
        because a status panel must never break the dashboard.
    """

    levels: List[Mapping[str, Any]] = [
        {
            "id": "configured",
            "label": "MCP server configured (project scope)",
            "detail": (
                "IBM Bob is pointed at this repository's read-only UPSHIFT "
                "evidence server by a project configuration file."
            ),
            **_probe_configuration(),
        },
        {
            "id": "local_mcp",
            "label": "Local MCP integration verified",
            "detail": (
                "The one documented read-only tool is declared by the server "
                "package, and the test suite asserts that declaration equals "
                "the tool list the running server registers."
            ),
            **_probe_declared_surface(),
        },
        {
            "id": "live_bob",
            "label": "Live IBM Bob runtime verified",
            "detail": "A real IBM Bob client session observed running this server.",
            "status": "not_verified",
            "evidence": _LIVE_BOB_REASON,
            "verified_by": None,
        },
    ]

    return {
        "summary": (
            "Configured and locally verified over local STDIO. No live IBM Bob "
            "runtime session has been observed, so none is claimed."
        ),
        "levels": list(levels),
        "bob_responsibility": BOB_RESPONSIBILITY,
        "upshift_responsibility": UPSHIFT_RESPONSIBILITY,
        "mcp_config_path": BOB_CONFIG_RELATIVE_PATH,
        "expected_tools": list(EXPECTED_MCP_TOOLS),
        "live_bob_runtime_verified": False,
        "executes_migration": False,
    }
