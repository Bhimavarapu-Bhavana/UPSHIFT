"""Console entry point for the local UPSHIFT dashboard.

Run it with::

    .venv\\Scripts\\python.exe -m app.api

Real-repository mode is closed by default. To enable it, allow one or more
directories on the command line::

    .venv\\Scripts\\python.exe -m app.api --allow-repository-root C:\\src\\service

The flag is repeatable, and it is *operator* configuration: only whoever starts
the process can widen the boundary, never a request. A repository is read only
when it resolves inside an allowed root. If an allowed root is not an existing
absolute local directory, the server refuses to start rather than analysing
something unexpected.

The server binds to loopback only, so the dashboard is reachable from this
machine and nothing else. It is an analysis view, not a public service.
"""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from app.api.http_app import create_dashboard_app
from app.security.repository_input import RepositoryInputError

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "build_parser", "main"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the local dashboard server."""

    parser = argparse.ArgumentParser(
        prog="python -m app.api",
        description="Serve the local UPSHIFT migration-safety dashboard.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="interface to bind (defaults to loopback)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="TCP port to bind",
    )
    parser.add_argument(
        "--allow-repository-root",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "absolute local directory inside which real-repository mode may read. "
            "Repeatable. Omit to keep real-repository mode closed"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the dashboard server and serve until interrupted."""

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "refusing to bind a non-loopback interface; the dashboard is a "
            "local analysis view"
        )
        return 2

    try:
        app = create_dashboard_app(args.allow_repository_root)
    except RepositoryInputError as error:
        print(f"refusing to start: {error}")
        return 2

    if app.state.allowed_repository_roots:
        count = len(app.state.allowed_repository_roots)
        print(
            f"real-repository mode enabled inside {count} allowed "
            f"{'root' if count == 1 else 'roots'}"
        )
    else:
        print(
            "real-repository mode is closed; pass --allow-repository-root to enable it"
        )

    import uvicorn

    print(f"UPSHIFT dashboard on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
