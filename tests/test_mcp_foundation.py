"""Focused tests for the UPSHIFT MCP foundation (Phase 8.1).

These tests cover the read-only, local STDIO MCP surface: import safety against
the official MCP SDK, transport configuration, the single
``get_migration_context`` tool, and the read-only security boundary that
separates UPSHIFT evidence from migration execution.
"""

import asyncio
import ast
import hashlib
import importlib
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer as OfficialMCPServer
from mcp.server.mcpserver.exceptions import ToolError

import upshift_mcp
import upshift_mcp.context as context_module
import upshift_mcp.server as server_module
from app.verification.verifier import UnknownCandidateError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "upshift_mcp"

EXCLUDED_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache"}

KNOWN_CANDIDATES = ("old_baseline", "correct_migration", "regression_migration")

#: Every top-level module ``upshift_mcp`` is permitted to import. The set is
#: intentionally minimal so that a network or execution capability cannot be
#: introduced without a test failure.
ALLOWED_IMPORT_ROOTS = {
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

#: Dynamic-execution and mutation primitives that must never be called.
FORBIDDEN_NAMES = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "input",
    "breakpoint",
    "system",
    "popen",
    "remove",
    "unlink",
    "rmtree",
    "rmdir",
    "rename",
    "replace",
    "makedirs",
    "chmod",
    "chown",
}

#: Alternative MCP transports and listeners that must not be referenced.
NETWORK_TOKENS = (
    "sse",
    "streamable",
    "streamable_http",
    "streamable-http",
    "uvicorn",
    "starlette",
    "FastAPI",
    "fastapi",
    "socket",
    "urllib",
    "httpx",
    "requests",
    "aiohttp",
    "websocket",
    "asgi",
    "wsgi",
    "bind",
    "listen",
    "localhost",
    "http",
    "https",
)

REQUIRED_CONTEXT_KEYS = {
    "migration_name",
    "candidate_id",
    "candidate_path",
    "old_api",
    "target_api",
    "read_only",
    "boundary",
    "impact",
    "risk",
    "verification",
    "decision",
}

REQUIRED_IMPACT_KEYS = {
    "impacted_files",
    "impacted_file_count",
    "direct_references",
    "direct_reference_count",
    "target_references",
    "target_reference_count",
}

REQUIRED_RISK_KEYS = {
    "risk_level",
    "risk_score",
    "impacted_file_count",
    "direct_old_reference_count",
    "target_reference_count",
    "affected_test_files",
    "risk_factors",
    "explanations",
}

REQUIRED_VERIFICATION_KEYS = {
    "verification_status",
    "total_cases",
    "passed_cases",
    "failed_cases",
    "inconclusive_cases",
    "skipped_cases",
    "results",
}

REQUIRED_DECISION_KEYS = {
    "decision_state",
    "accepted",
    "candidate_id",
    "total_cases",
    "passed_cases",
    "failed_cases",
    "inconclusive_cases",
    "skipped_cases",
    "explanations",
    "evidence",
}


def _package_sources():
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_ROOT.glob("*.py"))
    }


def _imported_roots(source):
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            roots.add(node.module.split(".")[0])
    return roots


def _called_names(source):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _code_only(source):
    """Return the source with comments and string literals removed."""

    kept = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    ]
    return " ".join(kept)


def _call_tool(name, arguments):
    return asyncio.run(server_module.server.call_tool(name, arguments))


async def _list_tool_names():
    tools = await server_module.server.list_tools()
    return [tool.name for tool in tools]


def _tool_names():
    return asyncio.run(_list_tool_names())


async def _tool_named(name):
    tools = await server_module.server.list_tools()
    for tool in tools:
        if tool.name == name:
            return tool
    return None


# A. The MCP server can be imported using the official SDK.


def test_mcp_server_imports_the_official_sdk():
    assert server_module.MCPServer is OfficialMCPServer
    assert issubclass(OfficialMCPServer, object)
    assert OfficialMCPServer.__module__ == "mcp.server.mcpserver.server"
    assert isinstance(server_module.server, OfficialMCPServer)
    assert server_module.server.name == server_module.SERVER_NAME
    assert server_module.server.version == server_module.SERVER_VERSION


# B. The local upshift_mcp package no longer shadows the official mcp SDK.


def test_upshift_package_does_not_shadow_the_official_sdk():
    import mcp
    import mcp.server
    from mcp.server.mcpserver import MCPServer as ResolvedMCPServer

    assert ResolvedMCPServer is OfficialMCPServer
    assert mcp.server is not None

    sdk_path = Path(mcp.__file__).resolve()
    assert "site-packages" in sdk_path.parts
    assert sdk_path.parent != REPOSITORY_ROOT
    assert not (REPOSITORY_ROOT / "mcp").exists()

    spec = importlib.util.find_spec("mcp")
    assert spec is not None and spec.origin is not None
    assert Path(spec.origin).resolve().parent != REPOSITORY_ROOT

    assert upshift_mcp.__file__ is not None
    assert Path(upshift_mcp.__file__).resolve().parent == PACKAGE_ROOT


def test_repository_root_no_longer_defines_an_mcp_package():
    assert not (REPOSITORY_ROOT / "mcp").exists()
    assert (REPOSITORY_ROOT / "upshift_mcp").is_dir()
    assert (REPOSITORY_ROOT / "upshift_mcp" / "__init__.py").is_file()
    assert (REPOSITORY_ROOT / "upshift_mcp" / "server.py").is_file()


# C. STDIO is the configured transport.


def test_stdio_is_the_configured_transport():
    assert server_module.TRANSPORT == "stdio"

    captured = []

    def fake_run(transport=None, **kwargs):
        captured.append(transport)

    original = server_module.server.run
    server_module.server.run = fake_run
    try:
        server_module.run_mcp_server()
    finally:
        server_module.server.run = original

    assert captured == ["stdio"]


# D. get_migration_context exists.


def test_get_migration_context_tool_exists_and_is_the_only_tool():
    assert server_module.MIGRATION_CONTEXT_TOOL == "get_migration_context"
    assert _tool_names() == ["get_migration_context"]


def test_get_migration_context_is_annotated_read_only():
    tool = asyncio.run(_tool_named("get_migration_context"))

    assert tool is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is False
    # Phase 11.1 added the operator-approved real-repository mode. The surface
    # stays a fixed, closed allowlist: a repository *name* and an
    # identifier-only declaration, never a path or a command.
    assert sorted(tool.input_schema["properties"]) == [
        "candidate_id",
        "migration",
        "repository",
    ]


# E. The tool returns structured read-only migration context.


def test_tool_returns_structured_read_only_migration_context():
    result = _call_tool("get_migration_context", {"candidate_id": "correct_migration"})

    assert result.is_error is False
    payload = result.structured_content["result"]

    assert REQUIRED_CONTEXT_KEYS <= set(payload)
    assert payload["read_only"] is True
    assert payload["migration_name"]
    assert payload["candidate_id"] == "correct_migration"
    assert payload["candidate_path"].endswith("profile_service.py")

    assert REQUIRED_IMPACT_KEYS <= set(payload["impact"])
    assert payload["impact"]["impacted_file_count"] >= 1
    assert isinstance(payload["impact"]["impacted_files"], list)
    assert isinstance(payload["impact"]["direct_references"], list)
    assert isinstance(payload["impact"]["target_references"], list)

    assert REQUIRED_RISK_KEYS <= set(payload["risk"])
    assert payload["risk"]["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
    assert isinstance(payload["risk"]["risk_score"], int)

    assert REQUIRED_VERIFICATION_KEYS <= set(payload["verification"])
    assert payload["verification"]["verification_status"] in {"PASS", "FAIL", "INCONCLUSIVE"}
    assert payload["verification"]["total_cases"] == 5
    assert payload["verification"]["passed_cases"] == 5
    assert payload["verification"]["failed_cases"] == 0

    assert REQUIRED_DECISION_KEYS <= set(payload["decision"])
    assert payload["decision"]["decision_state"] in {"ACCEPT", "REJECT", "INCONCLUSIVE"}
    assert payload["decision"]["candidate_id"] == "correct_migration"
    assert payload["decision"]["explanations"]


def test_tool_payload_is_json_serializable_and_matches_the_projection():
    result = _call_tool("get_migration_context", {})
    payload = result.structured_content["result"]

    assert json.loads(json.dumps(payload)) == payload
    assert payload == context_module.build_migration_context("correct_migration")
    assert payload["candidate_id"] == context_module.DEFAULT_CANDIDATE_ID


@pytest.mark.parametrize("candidate_id", KNOWN_CANDIDATES)
def test_tool_reports_real_evidence_for_every_known_candidate(candidate_id):
    payload = context_module.build_migration_context(candidate_id)

    assert payload["candidate_id"] == candidate_id
    assert payload["verification"]["total_cases"] == 5
    assert payload["decision"]["decision_state"] in {"ACCEPT", "REJECT", "INCONCLUSIVE"}


def test_regression_candidate_evidence_reports_a_failure():
    payload = context_module.build_migration_context("regression_migration")

    assert payload["verification"]["verification_status"] == "FAIL"
    assert payload["verification"]["failed_cases"] == 1
    assert payload["decision"]["decision_state"] == "REJECT"
    assert payload["decision"]["accepted"] is False
    assert payload["verification"]["failure_evidence"]


# F. The tool does not modify repository files.


def _repository_snapshot():
    snapshot = {}
    for path in REPOSITORY_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if EXCLUDED_DIRECTORIES & set(path.relative_to(REPOSITORY_ROOT).parts):
            continue
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def test_tool_does_not_modify_repository_files():
    _call_tool("get_migration_context", {"candidate_id": "correct_migration"})
    before = _repository_snapshot()

    for candidate_id in KNOWN_CANDIDATES:
        _call_tool("get_migration_context", {"candidate_id": candidate_id})

    assert _repository_snapshot() == before
    assert before


# G. The tool cannot execute arbitrary commands.


def test_tool_signature_exposes_no_execution_parameter():
    import inspect

    signature = inspect.signature(server_module.get_migration_context)
    assert list(signature.parameters) == ["candidate_id"]

    real_signature = inspect.signature(server_module.get_real_repository_context)
    assert list(real_signature.parameters) == ["repository", "migration"]

    schema = asyncio.run(_tool_named("get_migration_context")).input_schema
    assert sorted(schema["properties"]) == ["candidate_id", "migration", "repository"]
    # A repository is selected by an approved *name*; no parameter may carry a
    # filesystem path or a command.
    for forbidden in ("path", "root", "dir", "directory", "command", "shell", "code"):
        assert forbidden not in schema["properties"]


def test_upshift_package_declares_no_execution_primitives():
    for filename, source in _package_sources().items():
        assert not FORBIDDEN_NAMES & _called_names(source), (
            f"{filename} must not call {sorted(FORBIDDEN_NAMES)}"
        )
        for token in ("os.system", "os.popen", "os.exec", "os.spawn", "shell=True", "pty."):
            assert token not in source, f"{filename} must not contain {token!r}"


def test_upshift_package_imports_only_allowlisted_modules():
    for filename, source in _package_sources().items():
        imported = _imported_roots(source)
        assert imported <= ALLOWED_IMPORT_ROOTS, (
            f"{filename} imports {sorted(imported - ALLOWED_IMPORT_ROOTS)}"
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"candidate_id": "rm -rf /"},
        {"candidate_id": "__import__('os').system('echo pwned')"},
        {"candidate_id": "correct_migration; echo pwned"},
        {"candidate_id": "old_baseline && shutdown"},
    ],
)
def test_command_like_candidate_ids_are_rejected(arguments):
    with pytest.raises(UnknownCandidateError):
        server_module.get_migration_context(**arguments)


def test_extra_tool_arguments_are_ignored_and_inert():
    baseline = _call_tool("get_migration_context", {"candidate_id": "correct_migration"})
    before = _repository_snapshot()

    for arguments in (
        {"candidate_id": "correct_migration", "command": "whoami"},
        {"candidate_id": "correct_migration", "path": "C:/Windows/System32"},
        {"candidate_id": "correct_migration", "shell": "bash"},
        {"candidate_id": "correct_migration", "execute": True},
    ):
        result = _call_tool("get_migration_context", arguments)
        assert result.is_error is False
        assert result.structured_content == baseline.structured_content

    assert _repository_snapshot() == before

    schema = asyncio.run(_tool_named("get_migration_context")).input_schema
    for forbidden in ("command", "path", "shell", "execute", "file", "module", "code"):
        assert forbidden not in schema["properties"]


def test_rejected_candidate_is_a_deliberate_refusal_not_a_crash():
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    for candidate_id in ("../../etc/passwd", "unknown_candidate", ".env"):
        with pytest.raises(ToolError) as caught:
            _call_tool("get_migration_context", {"candidate_id": candidate_id})
        assert not isinstance(caught.value, UnexpectedToolError)
        assert type(caught.value) is ToolError
        assert "known candidates" in str(caught.value)


def test_unknown_tool_name_is_rejected():
    with pytest.raises(ToolError):
        _call_tool("execute_migration", {"candidate_id": "correct_migration"})


# H. The tool does not expose arbitrary filesystem access.


@pytest.mark.parametrize(
    "candidate_id",
    [
        "../../etc/passwd",
        "..\\..\\Windows\\System32\\drivers",
        "C:/Windows/System32/config/SAM",
        "/etc/shadow",
        ".env",
        "secrets",
        "demo/migration_benchmark/old/profile_service.py",
        "demo.migration_benchmark.old.profile_service",
        "",
        "unknown_candidate",
    ],
)
def test_path_like_candidate_ids_are_rejected(candidate_id):
    with pytest.raises(UnknownCandidateError):
        server_module.get_migration_context(candidate_id=candidate_id)


def test_context_module_reads_no_files_directly():
    for filename in ("context.py", "server.py"):
        source = _package_sources()[filename]
        for token in ("read_text", "read_bytes", "open(", "Path.cwd", "os.environ", "getenv"):
            assert token not in source, f"{filename} must not contain {token!r}"


def test_context_boundary_uses_the_controlled_benchmark_root():
    assert context_module.REPOSITORY_ROOT == REPOSITORY_ROOT
    assert context_module.known_candidate_ids() == KNOWN_CANDIDATES


def test_prohibited_capabilities_are_declared():
    declared = context_module.PROHIBITED_CAPABILITIES

    for capability in (
        "migration execution",
        "repository file modification",
        "shell or subprocess execution",
        "dynamic python execution",
        "arbitrary filesystem paths",
        "credential or secret access",
        ".env access",
        "network requests",
        "git state modification",
        "IBM Bob invocation",
        "rollback",
        "recovery",
        "replanning",
    ):
        assert capability in declared


# I. No HTTP/network transport is started.


def test_upshift_package_declares_no_network_transport():
    for filename, source in _package_sources().items():
        code = _code_only(source)
        for token in NETWORK_TOKENS:
            assert not re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", code
            ), f"{filename} must not reference {token!r} in executable code"


def test_no_alternative_transport_literal_is_used():
    banned = {"sse", "streamable-http", "streamable_http", "http", "https", "ws", "wss"}
    for filename, source in _package_sources().items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value.strip().lower() not in banned, (
                    f"{filename} must not name an alternative transport"
                )


def test_upshift_package_imports_no_network_module():
    network_roots = {
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "httpx",
        "requests",
        "aiohttp",
        "websockets",
        "starlette",
        "uvicorn",
        "fastapi",
        "flask",
        "xmlrpc",
        "ftplib",
        "smtplib",
        "telnetlib",
    }
    for filename, source in _package_sources().items():
        assert not _imported_roots(source) & network_roots, (
            f"{filename} imports a network module"
        )


def test_importing_the_server_starts_no_transport_and_opens_no_socket():
    probe = (
        "import socket\n"
        "class _Blocked(socket.socket):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise AssertionError('network socket created')\n"
        "socket.socket = _Blocked\n"
        "socket.create_connection = lambda *a, **k: (_ for _ in ()).throw("
        "AssertionError('network connection attempted'))\n"
        "from mcp.server.mcpserver import MCPServer\n"
        "def _blocked_run(self, *a, **k):\n"
        "    raise AssertionError('server transport started on import')\n"
        "MCPServer.run = _blocked_run\n"
        "import upshift_mcp.server as s\n"
        "assert s.TRANSPORT == 'stdio'\n"
        "print('OK')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout


def test_server_exposes_no_http_applications():
    for forbidden in ("sse_app", "streamable_http_app", "custom_route"):
        assert not hasattr(server_module, forbidden)
        assert not callable(getattr(server_module.server, forbidden, None)) or (
            forbidden not in server_module.__all__
        )


# J. Existing Phase 1-7 behavior remains unchanged.


@pytest.mark.parametrize(
    "module_name",
    [
        "app",
        "app.api",
        "app.core",
        "app.core.decision_engine",
        "app.core.impact_analyzer",
        "app.core.risk_analyzer",
        "app.security",
        "app.verification",
        "app.verification.profile_label_benchmark",
        "app.verification.verifier",
        "upshift_mcp",
        "upshift_mcp.context",
        "upshift_mcp.server",
    ],
)
def test_scaffold_and_phase_packages_remain_importable(module_name):
    assert importlib.import_module(module_name)


def test_phase_one_to_seven_public_constants_are_intact():
    from app.core import decision_engine, impact_analyzer, risk_analyzer
    from app.verification import verifier

    assert decision_engine.ACCEPT == "ACCEPT"
    assert decision_engine.REJECT == "REJECT"
    assert decision_engine.INCONCLUSIVE == "INCONCLUSIVE"
    assert verifier.PASS == "PASS"
    assert verifier.FAIL == "FAIL"
    assert risk_analyzer.LOW == "LOW"
    assert risk_analyzer.HIGH == "HIGH"
    assert impact_analyzer.UnsafeRepositoryPathError is not None
    assert decision_engine.MigrationDecisionEngine is not None
    assert risk_analyzer.RiskAnalyzer is not None
    assert impact_analyzer.ImpactAnalyzer is not None
    assert verifier.CandidateVerifier is not None


def _benchmark_reports(candidate_id):
    from app.core.decision_engine import MigrationDecisionEngine
    from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
    from app.core.risk_analyzer import RiskAnalyzer
    from app.verification.profile_label_benchmark import (
        BENCHMARK_ROOT,
        load_metadata,
        verify_candidate,
    )

    metadata = load_metadata()
    migration = MigrationDescription.from_metadata(metadata)
    impact_report = ImpactAnalyzer(BENCHMARK_ROOT.parents[2]).analyze(migration)
    risk_report = RiskAnalyzer().analyze(migration, impact_report)
    verification_report = verify_candidate(candidate_id)
    return impact_report, risk_report, verification_report


@pytest.mark.parametrize(
    ("candidate_id", "expected_decision", "expected_status"),
    [
        ("old_baseline", "ACCEPT", "PASS"),
        ("correct_migration", "ACCEPT", "PASS"),
        ("regression_migration", "REJECT", "FAIL"),
    ],
)
def test_phase_four_to_seven_decisions_are_unchanged(
    candidate_id, expected_decision, expected_status
):
    from app.core.decision_engine import MigrationDecisionEngine

    impact_report, risk_report, verification_report = _benchmark_reports(candidate_id)
    report = MigrationDecisionEngine().decide(
        impact_report, risk_report, verification_report
    )

    assert report.decision == expected_decision
    assert verification_report.status == expected_status
    assert verification_report.total_cases == 5
    assert impact_report.impacted_files
    assert risk_report.risk_level in {"LOW", "MEDIUM", "HIGH"}


def test_mcp_foundation_does_not_add_attributes_to_protected_modules():
    from app.core import decision_engine, impact_analyzer, risk_analyzer
    from app.verification import profile_label_benchmark, verifier

    protected = (
        impact_analyzer,
        risk_analyzer,
        decision_engine,
        verifier,
        profile_label_benchmark,
    )
    for module in protected:
        leaked = [
            name
            for name in dir(module)
            if "mcp" in name.lower() or name in {"server", "build_migration_context"}
        ]
        assert leaked == []
