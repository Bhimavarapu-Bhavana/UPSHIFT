"""Phase 11.2: the configured MCP entry starts on the project environment, and
the live-IBM-Bob claim stays honest.

Phase 11.2 had two jobs, and this file covers both.

**The interpreter defect.** ``.bob/mcp.json`` configured a bare ``python``.
IBM Bob launches ``command`` from Bob's own ``PATH`` and does not activate a
virtual environment first, so the interpreter that reached the server was
whatever ``python`` happened to be first on the machine. On this workstation that
is ``C:\\Program Files\\Python314\\python.exe``, which does not have the project's
``mcp==2.2.0`` dependency: the server died on import and never spoke MCP. The
whole suite passed only when a developer happened to have the virtual environment
activated, which is a property of the developer's shell, not of the product.

``.bob/mcp_launcher.py`` closes that gap. These tests pin the fix, and pin the
properties that make the fix safe rather than merely convenient: it is portable,
it names no machine-specific path, and it cannot be turned into a way to run
arbitrary code.

**The honesty requirement.** A live IBM Bob client session could not be observed
in this environment, so nothing here claims one. What these tests pin is the part
that must never drift: the integration status keeps reporting *configured* and
*locally verified* as established facts while reporting *live Bob* as unverified,
and configuration alone must never be able to promote it. The result is
environment-independent, so it holds on a machine that does have IBM Bob
installed, and the live claim is still made by a human after a real session -
never inferred.

Grouped A-H to match the phase brief.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BOB_DIRECTORY = REPOSITORY_ROOT / ".bob"
BOB_MCP_CONFIG = BOB_DIRECTORY / "mcp.json"
BOB_MCP_LAUNCHER = BOB_DIRECTORY / "mcp_launcher.py"

SERVER_NAME = "upshift-migration-evidence"
MIGRATION_CONTEXT_TOOL = "get_migration_context"

#: Import path of the official MCP SDK server class the project pins.
SDK_SERVER_MODULE = "mcp.server.mcpserver"

#: A Windows drive or a POSIX home: either would make a committed path
#: machine-specific rather than portable.
ABSOLUTE_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]|/(?:home|Users)/")

#: Separators used to split PATH, which differs by platform.
_PATH_SEPARATOR = ";" if sys.platform == "win32" else ":"

#: Wall-clock ceiling for one real MCP session. A session normally completes in
#: about 1.2s and the SDK's shutdown is bounded to roughly 4.5s, so a session
#: that overruns this is a transport stall rather than slow work, and is reported
#: as one. See the matching deadline in ``tests/test_bob_mcp_integration.py``.
SESSION_DEADLINE_SECONDS = 120.0


def _config():
    return json.loads(BOB_MCP_CONFIG.read_text(encoding="utf-8"))


def _entry():
    return _config()["mcpServers"][SERVER_NAME]


def _launcher():
    """Import the launcher by path. Importing it starts nothing."""

    spec = importlib.util.spec_from_file_location(
        "upshift_bob_mcp_launcher_112", BOB_MCP_LAUNCHER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _handshake(command, args, cwd=REPOSITORY_ROOT):
    """Run one real STDIO MCP session and return (init, tool names, call result)."""

    async def drive():
        parameters = StdioServerParameters(command=command, args=args, cwd=str(cwd))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                names = [tool.name for tool in listed.tools]
                result = await session.call_tool(
                    MIGRATION_CONTEXT_TOOL, {"candidate_id": "correct_migration"}
                )
                return init, names, result

    try:
        return asyncio.run(asyncio.wait_for(drive(), SESSION_DEADLINE_SECONDS))
    except TimeoutError as error:
        raise AssertionError(
            f"the configured MCP server did not answer within "
            f"{SESSION_DEADLINE_SECONDS:.0f}s; the STDIO transport stalled"
        ) from error


def _inspect_surface():
    """Discover the tool list and annotations over one real session."""

    async def drive():
        parameters = StdioServerParameters(
            command=_configured_command(),
            args=_configured_args(),
            cwd=str(REPOSITORY_ROOT),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool = (await session.list_tools()).tools[0]
                return tool.name, sorted(tool.input_schema.get("properties", {})), (
                    tool.annotations.read_only_hint,
                    tool.annotations.destructive_hint,
                    tool.annotations.open_world_hint,
                )

    return asyncio.run(asyncio.wait_for(drive(), SESSION_DEADLINE_SECONDS))


def _configured_command():
    return shutil.which(_entry()["command"]) or _entry()["command"]


def _configured_args():
    return [
        arg.replace("${workspaceFolder}", str(REPOSITORY_ROOT))
        for arg in _entry()["args"]
    ]


def _run_entry(command, *extra):
    """Run the configured entry with no request, and capture both streams."""

    return subprocess.run(
        [str(command), *_configured_args(), *extra],
        cwd=str(REPOSITORY_ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _interpreter_without_sdk():
    """A ``python`` a Bob process could reach, that lacks the MCP SDK.

    This is the exact condition the launcher exists to survive, so the tests that
    use it refuse to pass vacuously: the search returns ``None`` unless it finds
    one, and the caller skips.
    """

    here = Path(sys.executable).resolve()
    names = ("python.exe", "python") if sys.platform == "win32" else ("python", "python3")
    for directory in os.environ.get("PATH", "").split(_PATH_SEPARATOR):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            if not candidate.is_file() or candidate.resolve() == here:
                continue
            probe = subprocess.run(
                [
                    str(candidate),
                    "-c",
                    "import importlib.util, sys\n"
                    "try:\n"
                    f"    found = importlib.util.find_spec({SDK_SERVER_MODULE!r})\n"
                    "except Exception:\n"
                    "    found = None\n"
                    "sys.exit(0 if found else 3)\n",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if probe.returncode == 3:
                return candidate
    return None


@pytest.fixture
def path_python_without_sdk():
    interpreter = _interpreter_without_sdk()
    if interpreter is None:
        pytest.skip("no PATH python without the mcp sdk on this machine")
    return interpreter


# --------------------------------------------------------------------------
# A. The configuration and the launcher are portable
# --------------------------------------------------------------------------


def _code_only(path):
    """The file's code with comments and string literals removed."""

    source = path.read_text(encoding="utf-8")
    return " ".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_a_no_machine_specific_path_is_used():
    """A committed absolute path would break the integration on another machine.

    Comments and string literals are stripped first, so the launcher may still
    *describe* the defect in prose without that prose being mistaken for a path
    the product depends on.
    """

    for path in (BOB_MCP_CONFIG, BOB_MCP_LAUNCHER):
        text = path.read_text(encoding="utf-8")
        assert str(REPOSITORY_ROOT) not in text, f"{path.name} hardcodes this checkout"
        assert not ABSOLUTE_PATH_PATTERN.search(_code_only(path)), (
            f"{path.name} uses a machine-specific path"
        )


def test_a_the_launcher_derives_the_project_root_from_its_own_location():
    launcher = _launcher()

    assert launcher.PROJECT_ROOT == REPOSITORY_ROOT
    assert launcher.PROJECT_ROOT == BOB_MCP_LAUNCHER.resolve().parent.parent


def test_a_project_interpreter_candidates_are_project_relative():
    launcher = _launcher()

    assert launcher.PROJECT_INTERPRETER_CANDIDATES
    for relative in launcher.PROJECT_INTERPRETER_CANDIDATES:
        candidate = Path(relative)
        assert not candidate.is_absolute()
        assert candidate.parts[0] == ".venv"


def test_a_the_configuration_still_uses_the_documented_stdio_schema():
    entry = _entry()

    assert set(entry) <= {"command", "args", "cwd", "env", "alwaysAllow", "disabled"}
    assert "env" not in entry, "UPSHIFT needs no credentials and must not be given any"
    assert "alwaysAllow" not in entry, "every tool call stays a human approval step"
    assert entry["disabled"] is False
    assert entry["cwd"] == "${workspaceFolder}"
    assert not {"url", "type", "headers"} & set(entry)


# --------------------------------------------------------------------------
# B. The configured entry starts the real server on the project environment
# --------------------------------------------------------------------------


def test_b_the_configured_entry_resolves_inside_the_project():
    """The defect, stated as an assertion: never an interpreter from outside."""

    interpreter = _launcher().resolve_interpreter()

    assert interpreter is not None
    assert Path(interpreter).resolve().is_relative_to(REPOSITORY_ROOT), (
        f"the configured entry resolved outside the project to {interpreter}"
    )


def test_b_the_configured_entry_serves_the_official_sdk():
    interpreter = _launcher().resolve_interpreter()
    assert interpreter is not None

    probe = (
        "import json, mcp;"
        "from mcp.server.mcpserver import MCPServer;"
        "print(json.dumps({'file': mcp.__file__, 'module': MCPServer.__module__}))"
    )
    completed = subprocess.run(
        [str(interpreter), "-c", probe],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["module"] == "mcp.server.mcpserver.server"
    assert "site-packages" in Path(payload["file"]).parts


def test_b_a_path_python_without_the_sdk_still_starts_the_real_server(
    path_python_without_sdk,
):
    """The regression test for the defect itself.

    ``command`` is a bare ``python``. Here it is deliberately pointed at an
    interpreter that cannot import the SDK - exactly what IBM Bob would hand the
    server when no virtual environment was activated. The server must still come
    up, over a real handshake, exposing exactly one tool.
    """

    init, names, result = _handshake(str(path_python_without_sdk), _configured_args())

    assert init.server_info.name == SERVER_NAME
    assert names == [MIGRATION_CONTEXT_TOOL]
    assert result.is_error is False
    assert result.structured_content["result"]["read_only"] is True


def test_b_the_configured_entry_reports_the_interpreter_it_used(
    path_python_without_sdk,
):
    """The stderr trace is the evidence an operator needs when Bob shows no server."""

    completed = _run_entry(path_python_without_sdk)
    trace = completed.stderr

    assert "[upshift-mcp-launcher]" in trace
    assert str(path_python_without_sdk) in trace, "the trace must name the interpreter Bob launched"
    assert str(_launcher().PROJECT_ROOT) in trace


def test_b_the_entry_keeps_stdout_clear_for_the_protocol(path_python_without_sdk):
    """A launcher that printed to stdout would corrupt the MCP frame stream."""

    completed = _run_entry(path_python_without_sdk)

    # stdout carries MCP frames only. With no request written it must be empty:
    # no banner, no trace, no interpreter path.
    assert completed.stdout == ""
    assert str(REPOSITORY_ROOT) not in completed.stdout


# --------------------------------------------------------------------------
# C. The launcher cannot be turned into arbitrary execution
# --------------------------------------------------------------------------


def test_c_the_launcher_names_one_fixed_module():
    launcher = _launcher()

    assert launcher.SERVER_MODULE == "upshift_mcp"
    assert launcher._handoff_arguments() == ["-m", "upshift_mcp"]
    assert (REPOSITORY_ROOT / launcher.SERVER_MODULE).is_dir()


def test_c_extra_arguments_are_ignored_rather_than_forwarded(
    path_python_without_sdk,
):
    """Arguments appended to the configured entry must not reach Python.

    Were they forwarded, ``-c`` would take precedence over ``-m``, the process
    would run the supplied code, print the sentinel, and exit - and no MCP
    session would ever open.
    """

    sentinel = "UPSHIFT_LAUNCHER_SENTINEL_9f2c"
    completed = _run_entry(
        path_python_without_sdk, "-c", f"print('{sentinel}')", "--version"
    )

    assert sentinel not in completed.stdout
    assert sentinel not in completed.stderr


def test_c_the_launcher_serves_normally_despite_hostile_arguments(
    path_python_without_sdk,
):
    init, names, _ = _handshake(
        str(path_python_without_sdk),
        [*_configured_args(), "-c", "import os; os._exit(9)"],
    )

    assert init.server_info.name == SERVER_NAME
    assert names == [MIGRATION_CONTEXT_TOOL]


def test_c_the_launcher_never_reads_a_credential_or_the_environment():
    """No ``env`` access, no dotenv, no shell, no dynamic code.

    Comments and string literals are stripped before the scan, so prose that
    *describes* a prohibition is not mistaken for the capability itself.
    """

    source = BOB_MCP_LAUNCHER.read_text(encoding="utf-8")

    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {
        "__future__",
        "importlib",
        "pathlib",
        "subprocess",
        "sys",
        "typing",
        "upshift_mcp",
    }, f"the launcher imports {sorted(roots)}"

    code = _code_only(BOB_MCP_LAUNCHER)
    for forbidden in (
        "os.environ",
        "getenv",
        "shell=True",
        "eval(",
        "exec(",
        "__import__",
        "run(",
        "Popen",
    ):
        assert forbidden not in code, f"the launcher references {forbidden!r}"


def test_c_a_missing_project_interpreter_fails_loudly_rather_than_serving():
    """No usable interpreter is an error, never a reason to serve anyway."""

    launcher = _launcher()
    original = launcher.PROJECT_ROOT

    try:
        launcher.PROJECT_ROOT = REPOSITORY_ROOT / "no-such-project"
        assert launcher.find_project_interpreter() is None
    finally:
        launcher.PROJECT_ROOT = original

    assert launcher.find_project_interpreter() is not None


# --------------------------------------------------------------------------
# D. The tool surface is unchanged
# --------------------------------------------------------------------------


def test_d_the_configured_entry_exposes_exactly_one_tool():
    init, names, result = _handshake(_configured_command(), _configured_args())

    assert init.server_info.name == SERVER_NAME
    assert init.server_info.version
    assert names == [MIGRATION_CONTEXT_TOOL]
    assert result.is_error is False


def test_d_no_second_tool_and_no_new_parameter_appeared():
    name, properties, annotations = _inspect_surface()

    assert name == MIGRATION_CONTEXT_TOOL
    # The launcher is a process launcher: it cannot appear in the tool surface.
    assert properties == ["candidate_id", "migration", "repository"]
    assert annotations == (True, False, False)


# --------------------------------------------------------------------------
# E. The benchmark evidence is unchanged by any of this
# --------------------------------------------------------------------------


def test_e_the_evidence_through_the_configured_entry_is_the_frozen_benchmark():
    _, _, result = _handshake(_configured_command(), _configured_args())
    payload = result.structured_content["result"]

    assert payload["impact"]["impacted_file_count"] == 18
    assert payload["impact"]["direct_reference_count"] == 112
    assert payload["impact"]["target_reference_count"] == 116
    assert payload["risk"]["risk_level"] == "HIGH"
    assert payload["risk"]["risk_score"] == 266


def test_e_the_evidence_is_identical_started_both_ways():
    """Starting through the launcher must not change a single reported value."""

    interpreter = _launcher().resolve_interpreter()
    direct = _handshake(str(interpreter), ["-m", "upshift_mcp"])[2]
    launched = _handshake(_configured_command(), _configured_args())[2]

    assert direct.structured_content == launched.structured_content


# --------------------------------------------------------------------------
# F. Integration status: three levels, and the third stays unverified
# --------------------------------------------------------------------------


def _status():
    from app.api.integration_status import describe_integration

    return describe_integration()


def _levels():
    return {level["id"]: level for level in _status()["levels"]}


def test_f_configured_is_established_by_reading_the_real_file():
    level = _levels()["configured"]

    assert level["status"] == "configured"
    assert SERVER_NAME in level["declared_servers"]
    assert level["verified_by"] == "local file read"


def test_f_local_mcp_is_reported_as_verified():
    level = _levels()["local_mcp"]

    assert level["status"] == "verified"
    assert level["tools"] == [MIGRATION_CONTEXT_TOOL]
    assert level["transport"] == "stdio"


def test_f_live_bob_is_never_promoted_by_configuration():
    """A valid configuration must not be able to imply a live Bob session."""

    level = _levels()["live_bob"]

    assert level["status"] == "not_verified"
    assert level["verified_by"] is None
    assert "IBM Bob" in level["evidence"]


def test_f_the_three_levels_are_distinct_facts_in_increasing_strength():
    levels = _levels()

    assert list(levels) == ["configured", "local_mcp", "live_bob"]
    assert levels["configured"]["status"] == "configured"
    assert levels["local_mcp"]["status"] == "verified"
    assert levels["live_bob"]["status"] == "not_verified"


def test_f_the_summary_claims_no_live_bob_session_and_executes_nothing():
    described = _status()

    assert described["live_bob_runtime_verified"] is False
    assert described["executes_migration"] is False
    assert described["expected_tools"] == [MIGRATION_CONTEXT_TOOL]
    assert "No live IBM Bob" in described["summary"]
    assert "none is claimed" in described["summary"]


# --------------------------------------------------------------------------
# G. No live Bob runtime, and none is invented
# --------------------------------------------------------------------------


def test_g_the_live_bob_claim_is_never_inferred_from_the_environment():
    """The product must not auto-promote the live level, even where Bob exists.

    This is deliberately environment-independent. Probing for a Bob executable
    and flipping the status on the result would be exactly the optimistic default
    the status module is required not to have, so no such probe exists in the
    product, and this test is the reason one cannot be added quietly.
    """

    from app.api import integration_status

    source = Path(integration_status.__file__).read_text(encoding="utf-8")
    for forbidden in ("shutil.which", "subprocess", "bob.exe", "LIVE_BOB_AVAILABLE"):
        assert forbidden not in source, (
            f"integration_status references {forbidden!r}; the live level must stay "
            "a human-observed fact, never a probe result"
        )

    assert _status()["live_bob_runtime_verified"] is False


def test_g_no_bob_client_is_simulated_anywhere_in_the_project():
    """No fake Bob, no mocked Bob, and no helper pretending to be one.

    This file is excluded: it necessarily names the patterns it forbids.
    """

    for path in sorted((REPOSITORY_ROOT / "tests").glob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        for fabricated in ("FakeBob", "MockBob", "fake_bob", "mock_bob", "bob_stub"):
            assert fabricated not in text, f"{path.name} defines a {fabricated}"


def test_g_the_documentation_does_not_claim_a_live_bob_session():
    for path in sorted((REPOSITORY_ROOT / "docs").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "Bob connected",
            "Bob successfully",
            "live Bob verified",
            "verified against a live Bob",
            "Bob session completed",
        ):
            assert forbidden not in text, f"{path.name} claims {forbidden!r}"


# --------------------------------------------------------------------------
# H. Nothing here widened the boundary
# --------------------------------------------------------------------------


def test_h_the_project_still_approves_no_repository_by_default():
    shipped = REPOSITORY_ROOT / ".upshift" / "mcp-repository.json"

    assert not shipped.exists(), (
        "Phase 11.2 observed no live Bob runtime and approved no repository; a "
        "committed approval would widen the boundary with no live consumer"
    )


def test_h_the_launcher_adds_no_mcp_server_and_no_tool():
    """The launcher's only output is a process running the Phase 8 server."""

    import upshift_mcp.server as server_module

    tools = asyncio.run(server_module.server.list_tools())

    assert [tool.name for tool in tools] == [MIGRATION_CONTEXT_TOOL]
    assert server_module.TRANSPORT == "stdio"
    assert len(_config()["mcpServers"]) == 1
