"""Minimal local HTTP presentation layer for the UPSHIFT dashboard.

This module is a delivery adapter only. It contains no migration logic: every
value it returns was produced by the existing UPSHIFT engines through
:mod:`app.api.dashboard_service`.

Complete, intentionally small endpoint surface:

============================  ======  ==========================================
Route                         Method  Purpose
============================  ======  ==========================================
``/``                         GET     Serve the single-page dashboard document.
``/api/candidates``           GET     List the controlled candidate identifiers.
``/api/analyze``              POST    Run the pipeline for one input.
===========================  ======  ==========================================

``POST /api/analyze`` accepts exactly one of two request shapes, and no
combination of them:

* ``{"candidate_id": "..."}`` runs the controlled benchmark, through
  :func:`app.api.dashboard_service.analyze_candidate`.
* ``{"repository_path": "/abs/path", "migration": {...}}`` runs real-repository
  mode, through :func:`app.api.repository_service.analyze_repository`.

There is deliberately no third shape: a caller cannot pass a candidate *and* a
path, a migration *without* a path, or a path with a field the two services do
not accept. Anything else is a 400.

Security boundary enforced here:

* ``GET /api/candidates`` also carries a read-only ``integration`` block that
  keeps *configured*, *locally verified*, and *live Bob runtime verified*
  apart. It is a status report rather than a new capability, and it never
  claims a live IBM Bob session that was not actually observed. See
  :mod:`app.api.integration_status`.

* There is no shell, Python-execution, command, file-write, Git, credential, or
  network-out endpoint, and no generic ``/execute``-style route exists. Any
  other path returns 404 and any other method on these paths returns 405.
* ``POST /api/analyze`` accepts no other field. In the benchmark shape the one
  field is validated against a static allowlist before any engine runs. In the
  real-repository shape the path is validated against the operator-configured
  boundary in :mod:`app.security.repository_input` before any file is opened,
  and the migration declaration is reduced to dotted identifiers. There is no
  parameter for a module, a URL, or a command.
* The boundary itself is operator configuration held on the application, not
  caller input, so a request can never widen it. With no boundary configured
  the server is running with real-repository mode closed.
* The request body is size-capped and must be a JSON object, so the endpoint
  cannot be turned into a bulk or oversized input sink. The cap is unchanged,
  so a very long repository path is refused with 413 rather than admitted.
* An error response never echoes a filesystem path, a command, or a traceback.
* The HTML is served from one controlled file inside the package; no directory
  listing, static mount, or arbitrary file read is exposed.

The server binds to loopback by default. It is a local analysis dashboard, not
a public service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from app.api.dashboard_service import (
    analyze_candidate,
    controlled_candidate_ids,
    describe_service,
)
from app.api.integration_status import describe_integration
from app.api.repository_service import analyze_repository, describe_analysis_modes
from app.security.repository_input import RepositoryInputError, build_repository_boundary
from app.verification.verifier import UnknownCandidateError

__all__ = [
    "ANALYZE_REQUEST_SHAPES",
    "MAX_REQUEST_BYTES",
    "ROUTE_TABLE",
    "create_dashboard_app",
]

_DASHBOARD_DOCUMENT = Path(__file__).resolve().parent / "static" / "dashboard.html"

#: Hard cap on an analyze request body. The contract needs one short field.
MAX_REQUEST_BYTES = 4096

#: The two, and only two, accepted analyze request shapes.
_DEMO_FIELDS = frozenset({"candidate_id"})
_REPOSITORY_FIELDS = frozenset({"repository_path", "migration"})

#: Exported so the accepted surface can be asserted as a whole.
ANALYZE_REQUEST_SHAPES: Tuple[frozenset, ...] = (_DEMO_FIELDS, _REPOSITORY_FIELDS)

_GENERIC_ERROR = "the request could not be processed"
_UNKNOWN_FIELD = (
    "send exactly one of: {'candidate_id'} for the controlled benchmark, or "
    "{'repository_path', 'migration'} for real-repository mode"
)


class _JsonResponse(JSONResponse):
    """JSON response with a compact, deterministic encoding."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, sort_keys=True, default=str).encode("utf-8")


def _error(status_code: int, detail: str) -> JSONResponse:
    return _JsonResponse({"error": detail}, status_code=status_code)


async def dashboard_document(request: Request) -> Response:
    """Serve the single-page dashboard document."""

    try:
        body = _DASHBOARD_DOCUMENT.read_text(encoding="utf-8")
    except OSError:
        return _error(500, "the dashboard document is unavailable")
    return HTMLResponse(body)


async def candidates(request: Request) -> Response:
    """List the controlled candidates and describe the service boundary.

    This also carries the Bob/MCP integration status and the two analysis modes.
    They are attached here rather than exposed on new routes so the documented
    three-route surface stays exactly three routes.
    """

    service = describe_service()
    modes = describe_analysis_modes(request.app.state.allowed_repository_roots)
    return _JsonResponse(
        {
            "candidates": list(controlled_candidate_ids()),
            "default_candidate": controlled_candidate_ids()[0],
            "service": service["service"],
            "boundary": service["boundary"],
            "executes_migration": service["executes_migration"],
            "prohibited_capabilities": service["prohibited_capabilities"],
            "engines": service["engines"],
            "modes": modes,
            "integration": describe_integration(),
        }
    )


async def analyze(request: Request) -> Response:
    """Run the existing pipeline for one controlled candidate or repository.

    The body selects the mode: an allowlisted ``candidate_id`` runs the
    controlled benchmark, a ``repository_path`` plus a ``migration``
    declaration runs real-repository mode. No other combination is accepted.
    """

    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return _error(413, "request body is too large")

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(400, "request body must be a JSON object")

    if not isinstance(payload, dict):
        return _error(400, "request body must be a JSON object")

    fields = set(payload)
    if fields == _DEMO_FIELDS:
        return await _analyze_controlled_candidate(payload)
    if fields == _REPOSITORY_FIELDS:
        return await _analyze_real_repository(request, payload)
    return _error(400, _UNKNOWN_FIELD)


async def _analyze_controlled_candidate(payload: Dict[str, Any]) -> Response:
    """Run the controlled benchmark for one allowlisted candidate."""

    try:
        result = analyze_candidate(payload["candidate_id"])
    except UnknownCandidateError:
        known = ", ".join(controlled_candidate_ids())
        return _error(400, f"unknown candidate; choose one of: {known}")
    except Exception:  # noqa: BLE001 - never leak internals to the client
        return _error(500, _GENERIC_ERROR)

    return _JsonResponse(result.to_dict())


async def _analyze_real_repository(request: Request, payload: Dict[str, Any]) -> Response:
    """Run real-repository mode for one boundary-validated repository path."""

    try:
        result = analyze_repository(
            payload["repository_path"],
            payload["migration"],
            request.app.state.allowed_repository_roots,
        )
    except RepositoryInputError:
        # A refusal is a client-input problem. The message is written to be safe
        # to show and never echoes the path or declaration that was refused.
        return _error(400, "the repository request was refused; see the boundary policy")
    except Exception:  # noqa: BLE001 - never leak internals to the client
        return _error(500, _GENERIC_ERROR)

    return _JsonResponse(result.to_dict())


def _route_table() -> Tuple[Route, ...]:
    return (
        Route("/", dashboard_document, methods=["GET"], name="dashboard"),
        Route("/api/candidates", candidates, methods=["GET"], name="candidates"),
        Route("/api/analyze", analyze, methods=["POST"], name="analyze"),
    )


#: The full route table, exported so tests can assert the complete surface.
ROUTE_TABLE: Tuple[Route, ...] = _route_table()


def create_dashboard_app(
    allowed_repository_roots: Optional[Sequence[str]] = None,
) -> Starlette:
    """Build the ASGI application for the local dashboard.

    Args:
        allowed_repository_roots: Absolute local directory paths the operator
            explicitly allows real-repository mode to read inside. The default
            is ``None``, which is a *closed* boundary: real-repository mode is
            then unavailable and every repository request is refused. A root may
            be passed only by the process that starts the server, never by a
            request.

    Returns:
        A configured :class:`starlette.applications.Starlette` app exposing
        only the three documented routes. The framework body-size cap is set
        from :data:`MAX_REQUEST_BYTES` so an oversized analyze request is
        refused by the framework before any handler runs.

    Raises:
        RepositoryBoundaryError: If an allowed root is not an existing absolute
            local directory. The server therefore fails closed at start-up
            rather than silently analysing something unexpected.
    """

    # Built eagerly so a bad boundary stops start-up instead of failing per
    # request, and stored as a frozen value so no handler can change it.
    boundary = build_repository_boundary(allowed_repository_roots)

    app = Starlette(
        routes=list(ROUTE_TABLE),
        max_body_size=MAX_REQUEST_BYTES,
    )
    app.state.allowed_repository_roots = tuple(str(root) for root in boundary.roots)
    return app
