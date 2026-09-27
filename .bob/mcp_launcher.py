"""Project-local launcher that starts the UPSHIFT MCP server for IBM Bob.

Why this file exists
--------------------
``.bob/mcp.json`` has to name an interpreter IBM Bob can execute. Bob launches
``command`` directly, from Bob's own process, using Bob's ``PATH``. Bob does not
activate a virtual environment first, and it does not read a project manifest to
choose an interpreter. A bare ``python`` therefore resolves to whichever
interpreter happens to be first on the machine. On a typical Windows install that
is the system interpreter under ``C:\\Program Files\\Python3xx``, which does
**not** have the project's ``mcp==2.2.0`` dependency installed. The server then
dies during import, before it can speak a single MCP frame, and the only thing
standing between an operator and a working server is remembering to activate
``.venv`` in whatever shell happened to launch Bob.

This launcher removes that hidden prerequisite. Bob still runs a plain
``python <launcher>``; the launcher notices that the interpreter it was handed
cannot import the MCP SDK, starts the **project** interpreter it finds next to
this file instead, and the server comes up.

What this launcher deliberately does not do
-------------------------------------------
* It takes no arguments and honours none it is given. The module it starts is
  the constant :data:`SERVER_MODULE`, so it is not a general "run this module"
  helper and cannot be turned into arbitrary code execution. Extra arguments are
  ignored, not forwarded.
* It reads no environment variable, no credential, and no ``.env`` file.
* It is not an MCP server and not an MCP tool. After the hand-off the process is
  ``python -m upshift_mcp`` over local STDIO, which is exactly the server and the
  single read-only tool Phase 8 already shipped. The tool surface is unchanged.
* It is portable. The project root and the project interpreter are both derived
  from this file's own location, so no absolute, machine-specific path is
  committed and the configuration keeps working on another machine or another
  operating system.

Transport note: the MCP protocol travels over the child's **stdout**, so the
resolution trace below is written to **stderr** and cannot corrupt a session. It
is also the evidence an operator needs when a server "does not appear": it names
the interpreter Bob actually ended up using.

Why the hand-off is a child process and not ``os.execv``
----------------------------------------------------------
Replacing this process in place would be tidier, and it was the first attempt. It
does not work on Windows: ``os.execv`` mangles a program path that contains a
space, and a checkout under ``C:\\Users\\<user>\\...`` has one. The re-exec reached
an interpreter path with a duplicated tail and the server never started. So the
hand-off spawns the project interpreter and waits for it, with stdio inherited
rather than captured.

Inheriting stdio is what keeps the protocol intact: the child writes MCP frames
straight to the pipe Bob is reading, so there is no proxying loop to break. The
launcher writes nothing to stdout itself, and it only ever holds the same handles
the child holds, so it cannot steal a frame or withhold an end-of-file. The cost
is one extra idle process for the life of the session and an exit status that is
propagated rather than replaced, which is the right trade for a launcher whose
whole job is to be uninteresting.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

__all__ = [
    "PROJECT_INTERPRETER_CANDIDATES",
    "PROJECT_ROOT",
    "SERVER_MODULE",
    "find_project_interpreter",
    "main",
    "resolve_interpreter",
    "server_sdk_available",
]

#: The one module this launcher will ever start. Not a parameter, on purpose.
SERVER_MODULE = "upshift_mcp"

#: Import path of the official MCP SDK server class the project depends on.
SERVER_SDK_MODULE = "mcp.server.mcpserver"

#: The project root, derived from this file: ``<root>/.bob/mcp_launcher.py``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Where a project virtual environment keeps its interpreter. Listed Windows
#: first, then POSIX; only an existing file is used, so an unused or wrong-platform
#: entry is inert rather than an error.
PROJECT_INTERPRETER_CANDIDATES: Tuple[str, ...] = (
    ".venv/Scripts/python.exe",
    ".venv/bin/python",
    ".venv/Scripts/python",
)

_TRACE_PREFIX = "[upshift-mcp-launcher]"


def _trace(message: str) -> None:
    """Report the resolution decision on stderr, never on stdout."""

    print(f"{_TRACE_PREFIX} {message}", file=sys.stderr, flush=True)


def server_sdk_available() -> bool:
    """Return whether *this* interpreter can import the official MCP SDK.

    Only the spec is looked up; the module is located, not executed, so this
    cannot import repository content or run anything.
    """

    try:
        return importlib.util.find_spec(SERVER_SDK_MODULE) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def find_project_interpreter() -> Optional[Path]:
    """Return the project virtual environment's interpreter, if one exists.

    Returns:
        An existing interpreter inside the project, or ``None``. Candidates are
        project-relative, so nothing machine-specific is stored anywhere.
    """

    for relative in PROJECT_INTERPRETER_CANDIDATES:
        candidate = PROJECT_ROOT / relative
        if candidate.is_file():
            return candidate
    return None


def resolve_interpreter() -> Optional[Path]:
    """Return the interpreter that will actually serve MCP, or ``None``.

    ``None`` means no usable interpreter was found: neither the one this process
    was started with nor a project virtual environment. Callers must treat that
    as a failure, never as a reason to serve anyway.
    """

    if server_sdk_available():
        return Path(sys.executable)
    return find_project_interpreter()


def _handoff_arguments() -> List[str]:
    return ["-m", SERVER_MODULE]


def main(argv: Optional[List[str]] = None) -> int:
    """Start the UPSHIFT MCP server, handing off to the project interpreter.

    Args:
        argv: Present only so ``main`` matches a conventional entry point. It is
            never read: any argument supplied by a caller is ignored rather than
            forwarded, so this launcher cannot be used to start other code.

    Returns:
        The exit status of the server, or ``1`` when no interpreter able to serve
        MCP could be found. The server is always started with stdio inherited and
        never with a shell.
    """

    _trace(f"project root: {PROJECT_ROOT}")
    _trace(f"launched with: {sys.executable}")

    if server_sdk_available():
        _trace(f"mcp sdk available; serving {SERVER_MODULE} from this interpreter")
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from upshift_mcp.server import main as serve

        serve()
        return 0

    interpreter = find_project_interpreter()
    if interpreter is None:
        _trace(
            "no interpreter with the mcp sdk was found: this one lacks it and "
            "the project has no virtual environment at any of "
            + ", ".join(PROJECT_INTERPRETER_CANDIDATES)
        )
        _trace(
            f"create one and install {Path('requirements.txt').name}, then Bob can "
            "start the server with no further configuration"
        )
        return 1

    _trace(f"mcp sdk missing; starting project interpreter: {interpreter}")

    # ``-m`` resolves the package against the working directory, so the project
    # root is pinned rather than inherited. Bob already sets cwd to the workspace
    # folder, so this is a no-op in the normal case and a fix in the abnormal one.
    # ``shell`` stays False: this is a fixed argument vector, never a command line.
    return subprocess.call(
        [str(interpreter), *_handoff_arguments()],
        cwd=str(PROJECT_ROOT),
        shell=False,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
