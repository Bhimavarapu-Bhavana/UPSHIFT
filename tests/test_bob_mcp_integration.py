"""Focused tests for the controlled IBM Bob <-> UPSHIFT MCP integration.

These tests treat ``.bob/mcp.json`` as the IBM Bob project-scope MCP
configuration and exercise the integration the way Bob does: the configuration's
own ``command``, ``args``, and ``cwd`` are used to start the real UPSHIFT MCP
server as a child process, and the official MCP client SDK performs a real
STDIO handshake, discovers ``get_migration_context``, and calls it.

Phase 11.2 added ``.bob/mcp_launcher.py`` between those two halves. The
configured ``command`` is a bare ``python``, and IBM Bob launches it from Bob's
own ``PATH`` without activating a virtual environment - so the interpreter that
reaches the server is not the one holding ``mcp==2.2.0``. The launcher closes
that gap. These tests therefore start the server *through the configured entry*,
which is the only arrangement an operator will ever experience.

Nothing here mocks the MCP server or fabricates a Bob response. The Bob IDE or
Bob Shell runtime is not present in this environment, so these tests verify the
server side of the contract; the remaining manual step is documented in
``docs/bob-mcp-integration.md``.
"""

import asyncio
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver import MCPServer as OfficialMCPServer

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BOB_DIRECTORY = REPOSITORY_ROOT / ".bob"
BOB_MCP_CONFIG = BOB_DIRECTORY / "mcp.json"
BOB_MCP_LAUNCHER = BOB_DIRECTORY / "mcp_launcher.py"
BOB_RULES_DIRECTORY = BOB_DIRECTORY / "rules"

SERVER_NAME = "upshift-migration-evidence"
MIGRATION_CONTEXT_TOOL = "get_migration_context"

KNOWN_CANDIDATES = ("old_baseline", "correct_migration", "regression_migration")

EXCLUDED_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache"}

#: Transport keys that would mean a network listener instead of STDIO.
NETWORK_CONFIG_KEYS = {"url", "headers", "type", "endpoint", "host", "port"}

#: STDIO keys documented by IBM Bob for a local MCP server.
STDIO_KEYS = {"command", "args", "cwd", "env", "alwaysAllow", "disabled"}

#: Wall-clock ceiling for one real MCP session.
#:
#: A session normally completes in about 1.2s, and the SDK's own shutdown is
#: bounded to roughly 4.5s. Nothing in this file legitimately takes minutes, so a
#: session that does is a transport stall, not slow work. The deadline turns such
#: a stall into a named, immediate failure instead of a silent multi-minute hang:
#: Phase 11.2 measured one 492s session and 19 others at 2-4s in the same run, and
#: a harness that can hang for an hour cannot report that distinction.
SESSION_DEADLINE_SECONDS = 120.0


def _config():
    return json.loads(BOB_MCP_CONFIG.read_text(encoding="utf-8"))


def _server_entry():
    servers = _config()["mcpServers"]
    assert SERVER_NAME in servers, (
        f"{BOB_MCP_CONFIG.name} must register {SERVER_NAME!r}; found {sorted(servers)}"
    )
    return servers[SERVER_NAME]


def _resolved_cwd(entry):
    cwd = entry.get("cwd")
    assert cwd, "the Bob entry must pin a working directory for the server"
    return Path(cwd.replace("${workspaceFolder}", str(REPOSITORY_ROOT)))


def _resolved_command(entry):
    command = entry["command"]
    return shutil.which(command) or command


def _resolved_args(entry):
    return [arg.replace("${workspaceFolder}", str(REPOSITORY_ROOT)) for arg in entry["args"]]


def _launcher():
    """Import the project-local launcher exactly as the configuration names it.

    Loaded from its path rather than imported by name, because it deliberately
    lives outside every package on ``sys.path``: it is a Bob entry point, not
    part of the shipped library. Loading it does not start anything, because the
    module only serves when it is run as a script.
    """

    spec = importlib.util.spec_from_file_location("upshift_bob_mcp_launcher", BOB_MCP_LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _launch_parameters():
    entry = _server_entry()
    return StdioServerParameters(
        command=_resolved_command(entry),
        args=_resolved_args(entry),
        cwd=str(_resolved_cwd(entry)),
    )


def _repository_snapshot():
    snapshot = {}
    for path in REPOSITORY_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if EXCLUDED_DIRECTORIES & set(path.relative_to(REPOSITORY_ROOT).parts):
            continue
        snapshot[path.relative_to(REPOSITORY_ROOT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return snapshot


def _run_as_bob(coroutine_factory):
    """Run one real STDIO MCP session against the server Bob would start.

    Raises:
        AssertionError: If the session does not finish inside
            ``SESSION_DEADLINE_SECONDS``. A stalled transport is reported as a
            failure naming the server it could not talk to, never as a pass and
            never as a silent wait.
    """

    async def drive():
        async with stdio_client(_launch_parameters()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await coroutine_factory(session)

    try:
        return asyncio.run(asyncio.wait_for(drive(), SESSION_DEADLINE_SECONDS))
    except TimeoutError as error:
        raise AssertionError(
            f"the configured MCP server did not answer within "
            f"{SESSION_DEADLINE_SECONDS:.0f}s; the STDIO transport stalled rather "
            f"than the tool being slow"
        ) from error


def _error_text(result):
    """Concatenate the text blocks of a refused tool result."""

    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


# A. Bob-compatible MCP configuration points to the correct UPSHIFT entry point.


def test_bob_project_configuration_exists_and_is_version_shareable():
    assert BOB_MCP_CONFIG.is_file()
    assert BOB_MCP_CONFIG.parent.name == ".bob"
    assert BOB_MCP_CONFIG.parent.parent == REPOSITORY_ROOT


def test_bob_configuration_registers_the_upshift_server():
    config = _config()

    assert "mcpServers" in config
    assert SERVER_NAME in config["mcpServers"]


def test_bob_configuration_points_at_the_real_upshift_entry_point():
    entry = _server_entry()
    args = _resolved_args(entry)

    assert entry["command"].endswith("python") or entry["command"] == "python"
    # Phase 11.2: the configured argument is the project-local launcher, which is
    # what makes a bare `python` on Bob's PATH reach the project environment.
    # The launcher, in turn, starts the real module - never a placeholder.
    assert [Path(arg) for arg in args] == [BOB_MCP_LAUNCHER]

    launcher = _launcher()
    assert launcher.SERVER_MODULE == "upshift_mcp"
    assert launcher.PROJECT_ROOT == REPOSITORY_ROOT
    assert launcher._handoff_arguments() == ["-m", "upshift_mcp"]

    assert _resolved_cwd(entry) == REPOSITORY_ROOT
    assert entry.get("disabled") is False

    import upshift_mcp.__main__ as entry_point

    assert callable(entry_point.main)
    assert Path(entry_point.__file__).resolve() == REPOSITORY_ROOT / "upshift_mcp" / "__main__.py"


def test_bob_configuration_uses_only_documented_stdio_keys():
    for name, entry in _config()["mcpServers"].items():
        assert not NETWORK_CONFIG_KEYS & set(entry), (
            f"{name} must not declare a network transport: {sorted(entry)}"
        )
        assert set(entry) <= STDIO_KEYS, (
            f"{name} uses keys outside the IBM Bob STDIO schema: {sorted(set(entry) - STDIO_KEYS)}"
        )


def test_bob_configuration_does_not_auto_approve_or_inject_credentials():
    entry = _server_entry()

    # Human control: every tool call stays an explicit approval step.
    assert "alwaysAllow" not in entry
    assert not entry.get("env"), "UPSHIFT must not require or accept credentials"


# B. STDIO is used.


def test_bob_configuration_uses_stdio_only():
    entry = _server_entry()

    assert "url" not in entry
    assert "type" not in entry
    assert entry["command"]
    assert isinstance(entry["args"], list)

    import upshift_mcp.server as server_module

    assert server_module.TRANSPORT == "stdio"


# C. The configured server resolves to the official MCP SDK.


def test_configured_interpreter_resolves_the_official_mcp_sdk():
    """The interpreter the configured entry actually serves with has the SDK.

    Phase 11.2 changed what this asserts. It used to probe the bare ``command``
    from ``.bob/mcp.json``, which *was* the defect: on a machine whose PATH
    ``python`` is not the project environment, that probe failed, and so did every
    real server start. The probe now runs against the interpreter the configured
    entry resolves to, and additionally pins it inside this project, so a
    regression to "whatever python happens to be first" fails here.
    """

    interpreter = _launcher().resolve_interpreter()

    assert interpreter is not None, "the configured entry must resolve to an interpreter"
    assert REPOSITORY_ROOT in Path(interpreter).resolve().parents, (
        f"the configured entry must serve with the project environment, not {interpreter}"
    )

    probe = (
        "import mcp, mcp.server, json;"
        "from mcp.server.mcpserver import MCPServer;"
        "print(json.dumps({'file': mcp.__file__,"
        "'module': MCPServer.__module__, 'is_official': MCPServer is not None}))"
    )
    completed = subprocess.run(
        [str(interpreter), "-c", probe],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "site-packages" in Path(payload["file"]).parts
    assert payload["module"] == "mcp.server.mcpserver.server"

    import mcp

    assert Path(mcp.__file__).resolve() == Path(payload["file"]).resolve()


def test_bob_entry_point_does_not_shadow_the_official_sdk():
    import mcp

    assert not (REPOSITORY_ROOT / "mcp").exists()
    assert (REPOSITORY_ROOT / "upshift_mcp").is_dir()
    assert Path(mcp.__file__).resolve().parent != REPOSITORY_ROOT


# D. get_migration_context is discoverable through the Bob connection.


def test_bob_discovers_the_migration_context_tool():
    async def discover(session):
        listed = await session.list_tools()
        return [(tool.name, tool.annotations) for tool in listed.tools]

    discovered = _run_as_bob(discover)
    names = [name for name, _ in discovered]

    assert names == [MIGRATION_CONTEXT_TOOL]


def test_bob_connects_to_a_server_over_stdio():
    async def describe(session):
        return await session.initialize()

    init = _run_as_bob(describe)

    assert init.server_info.name == SERVER_NAME
    assert init.server_info.version
    assert MIGRATION_CONTEXT_TOOL in init.instructions


# E. The tool is marked read-only.


def test_discovered_tool_is_marked_read_only():
    async def annotate(session):
        listed = await session.list_tools()
        tool = listed.tools[0]
        return tool.annotations, sorted(tool.input_schema.get("properties", {}))

    annotations, properties = _run_as_bob(annotate)

    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.open_world_hint is False
    # Phase 11.1 added real-repository mode as two more inputs on the same
    # single tool. It is still one read-only tool with a closed allowlist: a
    # benchmark candidate, an approved repository name, and a declaration. No
    # filesystem path and no command is accepted.
    assert properties == ["candidate_id", "migration", "repository"]
    for forbidden in ("path", "root", "dir", "directory", "command", "shell", "code"):
        assert forbidden not in properties


# F. Unknown tools are rejected.


def test_unknown_tool_is_rejected_over_the_bob_connection():
    async def call_unknown(session):
        return await session.call_tool("execute_migration", {"candidate_id": "correct_migration"})

    result = _run_as_bob(call_unknown)

    assert result.is_error is True
    assert "execute_migration" in _error_text(result)


# G. Arbitrary command/path arguments are rejected.


@pytest.mark.parametrize(
    "candidate_id",
    [
        "../../etc/passwd",
        "C:/Windows/System32/config/SAM",
        "/etc/shadow",
        ".env",
        "rm -rf /",
        "old_baseline; shutdown",
        "not_a_candidate",
    ],
)
def test_arbitrary_command_or_path_arguments_are_rejected(candidate_id):
    async def call_bad(session):
        return await session.call_tool(MIGRATION_CONTEXT_TOOL, {"candidate_id": candidate_id})

    result = _run_as_bob(call_bad)

    assert result.is_error is True
    # A deliberate refusal naming the allowlist, not a generic server crash.
    text = _error_text(result)
    assert "known candidates" in text
    assert candidate_id in text


def test_execution_shaped_arguments_are_inert():
    before = _repository_snapshot()

    async def call_with_extras(session):
        return await session.call_tool(
            MIGRATION_CONTEXT_TOOL,
            {
                "candidate_id": "correct_migration",
                "command": "whoami",
                "path": "C:/Windows/System32",
                "shell": "bash",
            },
        )

    result = _run_as_bob(call_with_extras)

    assert result.is_error is False
    payload = result.structured_content["result"]
    assert payload["candidate_id"] == "correct_migration"
    assert _repository_snapshot() == before


# The real end-to-end workflow: Bob asks for evidence and gets it back.


@pytest.mark.parametrize("candidate_id", KNOWN_CANDIDATES)
def test_bob_receives_read_only_migration_evidence(candidate_id):
    async def call_tool(session):
        return await session.call_tool(MIGRATION_CONTEXT_TOOL, {"candidate_id": candidate_id})

    result = _run_as_bob(call_tool)

    assert result.is_error is False
    payload = result.structured_content["result"]

    assert payload["read_only"] is True
    assert payload["candidate_id"] == candidate_id
    assert payload["migration_name"]
    assert payload["impact"]["impacted_files"]
    assert payload["risk"]["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
    assert payload["verification"]["total_cases"] == 5
    assert payload["decision"]["decision_state"] in {"ACCEPT", "REJECT", "INCONCLUSIVE"}
    assert payload["decision"]["explanations"]


def test_bob_workflow_distinguishes_a_rejected_candidate():
    async def call_tool(session):
        return await session.call_tool(
            MIGRATION_CONTEXT_TOOL, {"candidate_id": "regression_migration"}
        )

    payload = _run_as_bob(call_tool).structured_content["result"]

    assert payload["verification"]["verification_status"] == "FAIL"
    assert payload["decision"]["decision_state"] == "REJECT"
    assert payload["decision"]["accepted"] is False


def test_evidence_is_deterministic_across_connections():
    async def call_tool(session):
        return await session.call_tool(MIGRATION_CONTEXT_TOOL, {"candidate_id": "correct_migration"})

    first = _run_as_bob(call_tool).structured_content["result"]
    second = _run_as_bob(call_tool).structured_content["result"]

    assert first == second


# H. The MCP integration cannot modify repository files.


def test_integration_does_not_modify_repository_files():
    _run_as_bob(
        lambda session: session.call_tool(
            MIGRATION_CONTEXT_TOOL, {"candidate_id": "correct_migration"}
        )
    )
    before = _repository_snapshot()

    for candidate_id in KNOWN_CANDIDATES:
        _run_as_bob(
            lambda session, cid=candidate_id: session.call_tool(
                MIGRATION_CONTEXT_TOOL, {"candidate_id": cid}
            )
        )

    assert _repository_snapshot() == before
    assert before


def test_upshift_package_still_declares_no_execution_or_network_primitives():
    import ast
    import io
    import tokenize

    allowed_imports = {
        "__future__",
        "app",
        "dataclasses",
        "importlib",
        "json",
        "mcp",
        "mcp_types",
        "pathlib",
        "typing",
    }
    forbidden_calls = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "system",
        "popen",
        "spawn",
        "fork",
        "remove",
        "unlink",
        "rmtree",
        "rename",
        "replace",
        "chmod",
        "chown",
        "mkdir",
        "makedirs",
        "write_text",
        "write_bytes",
    }
    forbidden_code = (
        "subprocess",
        "socket",
        "urllib",
        "httpx",
        "requests",
        "os.system",
        "os.environ",
        "getenv",
        "shell=True",
        "uvicorn",
        "starlette",
    )

    for path in sorted((REPOSITORY_ROOT / "upshift_mcp").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= allowed_imports, f"{path.name} imports {sorted(roots - allowed_imports)}"

        calls = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not calls & forbidden_calls, f"{path.name} calls {sorted(calls & forbidden_calls)}"

        code = " ".join(
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )
        for token in forbidden_code:
            assert token not in code, f"{path.name} references {token!r}"


# I. No HTTP/network listener is introduced.


def test_integration_introduces_no_network_listener():
    raw = BOB_MCP_CONFIG.read_text(encoding="utf-8")

    for token in ("http://", "https://", "localhost", "127.0.0.1", "0.0.0.0", "streamable"):
        assert token not in raw, f"{BOB_MCP_CONFIG.name} must not reference {token!r}"


def test_child_server_process_binds_no_listening_socket():
    """Start the real server with socket creation disabled.

    Phase 11.2 runs the probe with the interpreter the configured entry resolves
    to, which is the interpreter the child process is. The configured entry
    itself is exercised end to end by the discovery and call tests above, which
    complete a real STDIO session with this one.
    """

    interpreter = _launcher().resolve_interpreter()
    assert interpreter is not None

    prelude = (
        "import socket\n"
        "class _Blocked(socket.socket):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise AssertionError('listening socket created')\n"
        "    def bind(self, *a, **k):\n"
        "        raise AssertionError('bind attempted')\n"
        "socket.socket = _Blocked\n"
        "socket.create_server = lambda *a, **k: (_ for _ in ()).throw("
        "AssertionError('socket server created'))\n"
    )
    probe = prelude + (
        "import upshift_mcp.server as s\n"
        "assert s.TRANSPORT == 'stdio'\n"
        "print('NO_LISTENER')\n"
    )
    completed = subprocess.run(
        [str(interpreter), "-c", probe],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "NO_LISTENER" in completed.stdout


# Bob is told how to use the evidence, and told what it may not do.


def test_bob_project_rule_teaches_the_safe_workflow():
    rules = sorted(BOB_RULES_DIRECTORY.glob("*.md"))
    assert rules, f"expected a Bob project rule in {BOB_RULES_DIRECTORY}"

    text = "\n".join(rule.read_text(encoding="utf-8") for rule in rules)

    assert SERVER_NAME in text
    assert MIGRATION_CONTEXT_TOOL in text
    for boundary in ("ACCEPT", "REJECT", "INCONCLUSIVE", "verification"):
        assert boundary in text
    assert "not mark a migration successful on your own" in text


# J and K. Existing Phase 8.1 and Phase 1-7 suites remain passing.


def test_phase_eight_one_and_phase_one_to_seven_suites_still_pass():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_scaffold.py",
            "tests/test_impact_analyzer.py",
            "tests/test_risk_analyzer.py",
            "tests/test_verifier.py",
            "tests/test_decision_engine.py",
            "tests/test_mcp_foundation.py",
        ],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "failed" not in completed.stdout
    assert "error" not in completed.stdout.lower()


def test_upshift_mcp_server_is_still_the_official_sdk_class():
    import upshift_mcp.server as server_module

    assert server_module.MCPServer is OfficialMCPServer
    assert isinstance(server_module.server, OfficialMCPServer)
    assert server_module.MIGRATION_CONTEXT_TOOL == MIGRATION_CONTEXT_TOOL
