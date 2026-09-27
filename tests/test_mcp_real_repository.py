"""Phase 11.1: the MCP server can serve evidence from an approved real repository.

The bridge is deliberately small. An operator lists a directory in
``.upshift/mcp-repository.json``; the MCP caller selects it *by name* and supplies
an identifier-only migration declaration. Everything after that is the existing
machinery: :class:`app.repository.RealRepositoryInput` for the bounded read and
:func:`app.api.repository_service.analyze_repository` for the pipeline.

These tests pin the security properties that make that safe, and the dynamic
behaviour that makes it useful. They are grouped A-M to match the phase brief.

A. No approved repository -> closed boundary, no silent read of a current dir.
B. Approved temporary repository -> evidence from the real files on disk.
C. Editing a real file changes the evidence.
D. Adding a relevant file changes the evidence.
E. Removing a relevant file removes its evidence.
F. A repository outside the approved boundary is refused.
G. ``..`` traversal is refused.
H. A link that escapes the boundary is refused.
I. An oversized file is still refused by the existing size limit.
J. Unsupported and generated file types are still filtered out.
K. Repository contents are never executed.
L. Benchmark candidates behave exactly as before.
M. The tool surface stays intentionally limited.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

import upshift_mcp.repository_boundary as repository_boundary
from app.core.impact_analyzer import MigrationDescription
from app.security.repository_input import (
    RepositoryBoundaryError,
    RepositoryDeclarationError,
)
from upshift_mcp.context import build_migration_context, build_real_repository_context
from upshift_mcp.repository_boundary import (
    McpBoundaryConfigurationError,
    mcp_repository_boundary,
    mcp_repository_names,
    select_approved_root,
)
from upshift_mcp.server import get_migration_context
from app.verification.profile_label_benchmark import load_metadata

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The migration is derived from the real benchmark metadata rather than written
# out here. That keeps the controlled benchmark's own frozen Phase 4 evidence
# unchanged: this file then contains none of the migrated symbols, so it cannot
# become an impacted file of the benchmark it also tests.
_BENCHMARK = MigrationDescription.from_metadata(load_metadata())

MIGRATION_NAME = _BENCHMARK.name
OLD_API = _BENCHMARK.old_api
TARGET_API = _BENCHMARK.target_api

MIGRATION = {
    "name": MIGRATION_NAME,
    "old_api": OLD_API,
    "target_api": TARGET_API,
}

CONFIG_NAME = "mcp-repository.json"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write_config(config_root: Path, roots, extra=None) -> Path:
    """Write an operator approval file and point the module at it."""

    directory = config_root / ".upshift"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"allowed_repository_roots": [str(root) for root in roots]}
    if extra:
        payload.update(extra)
    path = directory / CONFIG_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _install(monkeypatch, config_root: Path, roots, extra=None) -> Path:
    path = _write_config(config_root, roots, extra)
    monkeypatch.setattr(repository_boundary, "MCP_REPOSITORY_CONFIG_PATH", path)
    return path


def _make_repository(root: Path) -> Path:
    """A small real repository holding a genuine old-API call site.

    The bodies are built from the benchmark's own entry points, so the fixture
    exercises the same migration the controlled benchmark analyses without
    hard-coding its symbols.
    """

    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "directory.py").write_text(
        f'"""Directory helpers."""\n\n\ndef render(entry):\n    return {OLD_API}\n',
        encoding="utf-8",
    )
    (root / "src" / "app.py").write_text(
        f"from src import directory\n\n\ndef label(entry):\n"
        f"    return directory.render(entry)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_directory.py").write_text(
        "from src.directory import render\n\n\ndef test_render():\n    assert render\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def approved(tmp_path, monkeypatch):
    """An operator-approved real repository plus its approval file."""

    repository = _make_repository(tmp_path / "approved-repo")
    _install(monkeypatch, tmp_path, [repository])
    return repository


# --------------------------------------------------------------------------
# A. No approved repository fails closed
# --------------------------------------------------------------------------


def test_a_absent_configuration_yields_a_closed_boundary(tmp_path, monkeypatch):
    missing = tmp_path / ".upshift" / CONFIG_NAME
    monkeypatch.setattr(repository_boundary, "MCP_REPOSITORY_CONFIG_PATH", missing)

    boundary = mcp_repository_boundary()

    assert boundary.is_configured is False
    assert mcp_repository_names() == ()


def test_a_closed_boundary_refuses_instead_of_reading_a_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        repository_boundary, "MCP_REPOSITORY_CONFIG_PATH", tmp_path / "absent.json"
    )

    with pytest.raises(RepositoryBoundaryError) as caught:
        build_real_repository_context(None, MIGRATION)

    assert "no repository boundary is configured" in str(caught.value)


def test_a_real_repository_mode_still_requires_a_declaration(approved):
    with pytest.raises(RepositoryDeclarationError):
        build_real_repository_context(approved.name, None)


def test_a_unknown_repository_name_is_refused_without_echoing_it(approved):
    with pytest.raises(RepositoryBoundaryError) as caught:
        build_real_repository_context("C:/Windows/System32", MIGRATION)

    message = str(caught.value)
    assert "unknown approved repository" in message
    assert "approved repositories: approved-repo" in message
    assert "System32" not in message


def test_a_malformed_approval_fails_loudly_rather_than_silently(tmp_path, monkeypatch):
    directory = tmp_path / ".upshift"
    directory.mkdir()
    path = directory / CONFIG_NAME
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(repository_boundary, "MCP_REPOSITORY_CONFIG_PATH", path)

    with pytest.raises(McpBoundaryConfigurationError):
        mcp_repository_boundary()


def test_a_approval_with_unexpected_keys_is_refused(tmp_path, monkeypatch):
    _install(monkeypatch, tmp_path, [], extra={"allow_all": True})

    with pytest.raises(McpBoundaryConfigurationError) as caught:
        mcp_repository_boundary()

    assert "accepts only" in str(caught.value)


def test_a_approval_naming_a_missing_directory_is_refused(tmp_path, monkeypatch):
    _install(monkeypatch, tmp_path, [tmp_path / "does-not-exist"])

    with pytest.raises(RepositoryBoundaryError):
        mcp_repository_boundary()


# --------------------------------------------------------------------------
# B. An approved repository is read for real
# --------------------------------------------------------------------------


def test_b_evidence_reports_real_repository_mode_and_real_files(approved):
    payload = build_real_repository_context(approved.name, MIGRATION)

    assert payload["mode"] == "real_repository"
    assert payload["read_only"] is True
    assert payload["repository"]["repository_name"] == "approved-repo"
    assert payload["repository"]["inspected_paths"] == [
        "src/app.py",
        "src/directory.py",
        "tests/test_directory.py",
    ]
    assert payload["impact"]["impacted_files"]
    assert payload["impact"]["direct_reference_count"] >= 1
    assert payload["risk"]["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
    assert payload["verification"]["verification_status"] == "INCONCLUSIVE"
    assert payload["decision"]["decision"] == "INCONCLUSIVE"
    assert payload["limitations"]


def test_b_result_never_discloses_the_absolute_repository_path(approved):
    payload = build_real_repository_context(approved.name, MIGRATION)
    serialized = json.dumps(payload)

    assert str(approved) not in serialized
    for path in payload["repository"]["inspected_paths"]:
        assert not Path(path).is_absolute()


def test_b_evidence_never_claims_a_real_repository_was_accepted(approved):
    payload = build_real_repository_context(approved.name, MIGRATION)

    assert payload["decision"]["accepted"] is False
    assert payload["recovery"]["migration_accepted"] is False
    assert payload["final_outcome"] == "MIGRATION_INCONCLUSIVE"


def test_b_relative_approval_paths_resolve_against_the_project_root(tmp_path, monkeypatch):
    """A committed approval stays portable: a relative root is resolved, not refused."""

    repository = _make_repository(tmp_path / "project" / "vendor-repo")
    config_root = tmp_path / "project"
    directory = config_root / ".upshift"
    directory.mkdir(parents=True)
    path = directory / CONFIG_NAME
    path.write_text(
        json.dumps({"allowed_repository_roots": ["vendor-repo"]}), encoding="utf-8"
    )
    monkeypatch.setattr(repository_boundary, "MCP_REPOSITORY_CONFIG_PATH", path)

    payload = build_real_repository_context("vendor-repo", MIGRATION)

    assert payload["repository"]["repository_name"] == "vendor-repo"
    assert "src/directory.py" in payload["repository"]["inspected_paths"]


def test_b_several_approved_repositories_require_a_name(tmp_path, monkeypatch):
    first = _make_repository(tmp_path / "repo-one")
    second = _make_repository(tmp_path / "repo-two")
    _install(monkeypatch, tmp_path, [first, second])

    assert mcp_repository_names() == ("repo-one", "repo-two")
    with pytest.raises(RepositoryBoundaryError) as caught:
        build_real_repository_context(None, MIGRATION)
    assert "one must be named" in str(caught.value)

    assert build_real_repository_context("repo-two", MIGRATION)["repository"][
        "repository_name"
    ] == "repo-two"


# --------------------------------------------------------------------------
# C-E. The evidence is dynamic, not canned
# --------------------------------------------------------------------------


def test_c_editing_a_real_file_changes_the_evidence(approved):
    before = build_real_repository_context(approved.name, MIGRATION)

    (approved / "src" / "directory.py").write_text(
        f'"""Directory helpers."""\n\n\ndef render(entry):\n    return {OLD_API}\n'
        f"\n\ndef badge(entry):\n    return {OLD_API}\n",
        encoding="utf-8",
    )
    after = build_real_repository_context(approved.name, MIGRATION)

    # The exact delta belongs to the Phase 4 analyzer, not to this bridge; the
    # property that matters is that the evidence is recomputed from the file.
    assert after["impact"]["direct_reference_count"] > before["impact"][
        "direct_reference_count"
    ]
    assert after["repository"]["inspected_file_count"] == before["repository"][
        "inspected_file_count"
    ]
    assert after != before


def test_d_adding_a_relevant_file_changes_the_evidence(approved):
    before = build_real_repository_context(approved.name, MIGRATION)

    (approved / "src" / "badge.py").write_text(
        f"def badge(entry):\n    return {OLD_API}\n", encoding="utf-8"
    )
    after = build_real_repository_context(approved.name, MIGRATION)

    assert "src/badge.py" in after["repository"]["inspected_paths"]
    assert "src/badge.py" not in before["repository"]["inspected_paths"]
    assert after["repository"]["inspected_file_count"] == before["repository"][
        "inspected_file_count"
    ] + 1


def test_e_removing_a_relevant_file_removes_its_evidence(approved):
    before = build_real_repository_context(approved.name, MIGRATION)
    assert "src/directory.py" in before["repository"]["inspected_paths"]
    assert before["impact"]["direct_reference_count"] >= 1

    (approved / "src" / "directory.py").unlink()
    after = build_real_repository_context(approved.name, MIGRATION)

    assert "src/directory.py" not in after["repository"]["inspected_paths"]
    assert "src/directory.py" not in after["impact"]["impacted_files"]
    assert after["impact"]["direct_reference_count"] < before["impact"][
        "direct_reference_count"
    ]


# --------------------------------------------------------------------------
# F-G. Outside the boundary
# --------------------------------------------------------------------------


def test_f_a_repository_outside_the_boundary_is_refused(tmp_path, monkeypatch):
    approved = _make_repository(tmp_path / "approved-repo")
    outsider = _make_repository(tmp_path / "other-repo")
    _install(monkeypatch, tmp_path, [approved])

    with pytest.raises(RepositoryBoundaryError) as caught:
        build_real_repository_context(str(outsider), MIGRATION)

    assert "unknown approved repository" in str(caught.value)
    assert "other-repo" not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        "../../etc",
        "..\\..\\Windows",
        "approved-repo/../..",
        "approved-repo/../other-repo",
        ".",
        "..",
        "/etc/shadow",
        "C:/Windows/System32/config/SAM",
    ],
)
def test_g_traversal_and_path_shaped_names_are_refused(approved, name):
    with pytest.raises(RepositoryBoundaryError):
        build_real_repository_context(name, MIGRATION)


def test_g_a_name_is_an_identifier_not_a_path(approved):
    """The approved name is matched literally; a path never resolves to a root."""

    boundary = mcp_repository_boundary()

    with pytest.raises(RepositoryBoundaryError):
        select_approved_root(boundary, "approved-repo/src")
    with pytest.raises(RepositoryBoundaryError):
        select_approved_root(boundary, 42)


# --------------------------------------------------------------------------
# H. Link escape
# --------------------------------------------------------------------------


def _link_reasons(payload) -> set:
    return {summary["reason"] for summary in payload["repository"]["skipped"]}


def test_h_a_symlink_pointing_outside_the_boundary_is_not_followed(tmp_path, monkeypatch):
    approved = _make_repository(tmp_path / "approved-repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("token = 'do-not-read'\n", encoding="utf-8")
    _install(monkeypatch, tmp_path, [approved])

    link = approved / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("directory symlinks are not available on this platform")

    payload = build_real_repository_context(approved.name, MIGRATION)
    serialized = json.dumps(payload)

    assert "secret.py" not in serialized
    assert "do-not-read" not in serialized
    assert "escape" not in payload["repository"]["inspected_paths"]
    assert _link_reasons(payload)


def test_h_a_windows_junction_out_of_the_boundary_is_not_followed(tmp_path, monkeypatch):
    """Junctions are the escape a non-admin Windows account can actually create."""

    import subprocess

    approved = _make_repository(tmp_path / "approved-repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("token = 'do-not-read'\n", encoding="utf-8")
    _install(monkeypatch, tmp_path, [approved])

    link = approved / "escape"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"junctions are not available here: {completed.stderr.strip()}")

    payload = build_real_repository_context(approved.name, MIGRATION)
    serialized = json.dumps(payload)

    assert "secret.py" not in serialized
    assert "do-not-read" not in serialized
    assert "escape" not in payload["repository"]["inspected_paths"]
    assert _link_reasons(payload)


# --------------------------------------------------------------------------
# I-J. Existing reader limits still apply
# --------------------------------------------------------------------------


def test_i_an_oversized_file_is_still_refused(tmp_path, monkeypatch):
    approved = _make_repository(tmp_path / "approved-repo")
    _install(monkeypatch, tmp_path, [approved])

    limits = build_real_repository_context(approved.name, MIGRATION)["repository"][
        "limits"
    ]
    (approved / "src" / "huge.py").write_text(
        "x = 1\n" * (limits["max_file_bytes"]), encoding="utf-8"
    )

    payload = build_real_repository_context(approved.name, MIGRATION)

    assert "src/huge.py" not in payload["repository"]["inspected_paths"]
    reasons = {summary["reason"] for summary in payload["repository"]["skipped"]}
    assert reasons & {
        "file_too_large",
        "max_file_bytes",
        "oversized_file",
        "size",
        "too_large",
    }


@pytest.mark.parametrize(
    "name",
    [
        "src/image.png",
        "src/archive.zip",
        "src/data.bin",
        "node_modules/pkg/index.js",
        ".git/config",
        "src/app.min.js",
        "src/secret.env",
        "src/keys/private.pem",
        "src/keystore.jks",
        "build/copy.py",
    ],
)
def test_j_unsupported_and_generated_files_remain_filtered(approved, name):
    """The existing reader policy is unchanged: text suffixes in, everything else out."""

    target = approved / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{OLD_API}\n", encoding="utf-8")

    payload = build_real_repository_context(approved.name, MIGRATION)

    assert name not in payload["repository"]["inspected_paths"]
    assert name not in payload["impact"]["impacted_files"]


# --------------------------------------------------------------------------
# K. Nothing in the repository is executed
# --------------------------------------------------------------------------


def test_k_repository_contents_are_never_imported_or_executed(approved):
    (approved / "src" / "tripwire.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "Path(__file__).with_name('TRIPWIRE_FIRED').write_text('executed')\n"
        "raise SystemExit('executed')\n",
        encoding="utf-8",
    )

    payload = build_real_repository_context(approved.name, MIGRATION)

    assert not (approved / "src" / "TRIPWIRE_FIRED").exists()
    assert not (approved / "src" / "__pycache__").exists()
    assert not list(approved.rglob("*.pyc"))
    assert "TRIPWIRE_FIRED" not in json.dumps(payload)


def test_k_the_repository_is_never_modified(approved):
    before = {
        path.relative_to(approved).as_posix(): path.read_bytes()
        for path in sorted(approved.rglob("*"))
        if path.is_file()
    }

    build_real_repository_context(approved.name, MIGRATION)

    after = {
        path.relative_to(approved).as_posix(): path.read_bytes()
        for path in sorted(approved.rglob("*"))
        if path.is_file()
    }
    assert after == before


# --------------------------------------------------------------------------
# L. Benchmark mode is untouched
# --------------------------------------------------------------------------


def test_l_benchmark_candidates_behave_exactly_as_before():
    for candidate_id in ("old_baseline", "correct_migration", "regression_migration"):
        payload = get_migration_context(candidate_id)
        assert "mode" not in payload
        assert payload["candidate_id"] == candidate_id
        assert payload["read_only"] is True
        assert payload["verification"]["total_cases"] == 5

    accepted = get_migration_context("correct_migration")
    rejected = get_migration_context("regression_migration")

    assert accepted["decision"]["decision_state"] == "ACCEPT"
    assert rejected["decision"]["decision_state"] == "REJECT"
    assert rejected["verification"]["verification_status"] == "FAIL"
    assert rejected["verification"]["passed_cases"] == 4
    assert rejected["verification"]["failed_cases"] == 1


def test_l_approving_a_repository_does_not_change_benchmark_results(approved):
    before = build_migration_context("correct_migration")
    after = build_real_repository_context(approved.name, MIGRATION)
    assert build_migration_context("correct_migration") == before
    assert after["mode"] == "real_repository"


def test_l_an_unknown_candidate_is_still_a_deliberate_refusal():
    from upshift_mcp.context import UnknownCandidateError

    with pytest.raises(UnknownCandidateError) as caught:
        build_migration_context("not_a_candidate")
    assert "known candidates" in str(caught.value)


# --------------------------------------------------------------------------
# M. The tool surface stays limited
# --------------------------------------------------------------------------


def test_m_server_registers_exactly_one_tool():
    import asyncio

    import upshift_mcp.server as server_module

    tools = asyncio.run(server_module.server.list_tools())

    assert [tool.name for tool in tools] == ["get_migration_context"]


def test_m_tool_exposes_no_path_command_or_execution_parameter():
    import asyncio
    import inspect

    import upshift_mcp.server as server_module

    signature = inspect.signature(server_module.get_migration_context)
    assert list(signature.parameters) == ["candidate_id"]

    tools = asyncio.run(server_module.server.list_tools())
    properties = tools[0].input_schema["properties"]

    assert sorted(properties) == ["candidate_id", "migration", "repository"]
    for forbidden in (
        "command",
        "path",
        "shell",
        "execute",
        "file",
        "module",
        "code",
        "root",
        "dir",
        "directory",
        "url",
        "env",
        "token",
    ):
        assert forbidden not in properties


def test_m_tool_remains_annotated_read_only():
    import asyncio

    import upshift_mcp.server as server_module

    tool = asyncio.run(server_module.server.list_tools())[0]

    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is False


def test_m_package_still_declares_no_execution_or_environment_primitives():
    """The bridge must not have introduced a shell, an interpreter, or env access.

    Comments and string literals are stripped first, so prose that *describes* a
    prohibition is not mistaken for the capability itself.
    """

    import ast
    import io
    import tokenize

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

    root = Path(__file__).resolve().parents[1] / "upshift_mcp"
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")

        calls = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        assert not calls & forbidden_calls, (
            f"{path.name} calls {sorted(calls & forbidden_calls)}"
        )

        code = " ".join(
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )
        for token in forbidden_code:
            assert token not in code, f"{path.name} references {token!r}"


def test_m_real_repository_mode_is_reachable_without_a_boundary_config(tmp_path, monkeypatch):
    """Sanity: the default project ships no approval, so the default is closed."""

    shipped = Path(__file__).resolve().parents[1] / ".upshift" / CONFIG_NAME
    assert not shipped.exists(), (
        "no repository may be approved by default; an operator must add one"
    )
    assert not mcp_repository_boundary().is_configured


# --------------------------------------------------------------------------
# Over the real STDIO connection
# --------------------------------------------------------------------------


def _call_over_stdio(arguments):
    """Call the registered tool through a real MCP STDIO session."""

    import sys

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def call():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "upshift_mcp"],
            cwd=str(REPOSITORY_ROOT),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("get_migration_context", arguments)

    return asyncio.run(call())


@contextlib.contextmanager
def _operator_approval(roots):
    """Write the real operator approval file a child MCP process will read.

    This is the production path: the child is a separate interpreter, so it
    resolves the same project-local approval an operator would create. The file
    is removed afterwards, and an existing operator approval is never
    overwritten.
    """

    config = REPOSITORY_ROOT / ".upshift" / CONFIG_NAME
    if config.exists():
        pytest.skip("an operator approval is already present; refusing to replace it")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"allowed_repository_roots": [str(root) for root in roots]}),
        encoding="utf-8",
    )
    try:
        yield config
    finally:
        config.unlink(missing_ok=True)
        try:
            config.parent.rmdir()
        except OSError:
            pass


@pytest.fixture(scope="session", autouse=True)
def _no_stale_operator_approval():
    """Remove a *test* approval left behind by an interrupted run.

    ``_operator_approval`` has to write into the real project root, because the
    child MCP process resolves the production configuration path. Its cleanup
    lives in a ``finally``, which an interrupted run never reaches - and a
    surviving ``.upshift/mcp-repository.json`` is not merely untidy: it silently
    reopens the read boundary for every later run, so other suites observe a
    configured boundary and fail in ways that look like product defects.

    Only a file that clearly belongs to a test is removed. A real approval names
    a repository a person chose, and it is left strictly alone.
    """

    config = REPOSITORY_ROOT / ".upshift" / CONFIG_NAME
    if not config.exists():
        yield
        return

    try:
        roots = json.loads(config.read_text(encoding="utf-8"))["allowed_repository_roots"]
    except (OSError, ValueError, KeyError, TypeError):
        pytest.fail(
            f"{config} exists and is not a readable approval this suite wrote; "
            "remove it by hand once the stale entry is gone"
        )

    stale = [root for root in roots if _is_test_temporary_path(root)]
    if len(stale) != len(roots) or not stale:
        yield
        return

    config.unlink()
    try:
        config.parent.rmdir()
    except OSError:
        pass
    yield


def _is_test_temporary_path(root: str) -> bool:
    """Whether a path belongs to a pytest temporary directory."""

    parts = Path(root).parts
    return "pytest-of-" in parts and "pytest-" in parts


def test_over_stdio_an_approved_name_returns_real_repository_evidence(tmp_path):
    """The bridge works over the wire, which is the path an agent client uses."""

    repository = _make_repository(tmp_path / "approved-repo")

    with _operator_approval([repository]):
        result = _call_over_stdio(
            {"repository": "approved-repo", "migration": MIGRATION}
        )

    assert result.is_error is False
    payload = result.structured_content["result"]
    assert payload["mode"] == "real_repository"
    assert payload["repository"]["inspected_paths"]
    assert payload["read_only"] is True


def test_over_stdio_a_path_in_the_repository_field_is_refused(tmp_path):
    """The wire surface refuses a path even when a real root is approved."""

    repository = _make_repository(tmp_path / "approved-repo")

    with _operator_approval([repository]):
        result = _call_over_stdio(
            {"repository": str(repository), "migration": MIGRATION}
        )

    assert result.is_error is True
    text = result.content[0].text
    assert "unknown approved repository" in text
    assert str(repository) not in text


def test_over_stdio_traversal_is_refused(tmp_path):
    """``..`` never reaches the reader, even with a real root approved."""

    repository = _make_repository(tmp_path / "approved-repo")

    with _operator_approval([repository]):
        result = _call_over_stdio(
            {"repository": "approved-repo/../..", "migration": MIGRATION}
        )

    assert result.is_error is True
    assert "unknown approved repository" in result.content[0].text


def test_over_stdio_with_no_approval_the_benchmark_still_works():
    """No approval file at all: the controlled benchmark is unchanged."""

    result = _call_over_stdio({"candidate_id": "regression_migration"})

    assert result.is_error is False
    payload = result.structured_content["result"]
    assert payload["candidate_id"] == "regression_migration"
    assert payload["decision"]["decision_state"] == "REJECT"


def test_over_stdio_real_repository_mode_without_a_approval_is_refused():
    """Requesting a repository with no approval fails closed on the wire."""

    result = _call_over_stdio({"repository": "anything", "migration": MIGRATION})

    assert result.is_error is True
    text = result.content[0].text
    assert "no repository boundary is configured" in text


def test_over_stdio_a_declaration_with_a_path_is_refused(tmp_path):
    """The declaration is identifier-only, so it cannot smuggle a path."""

    repository = _make_repository(tmp_path / "approved-repo")
    hostile = {
        "name": MIGRATION_NAME,
        "old_api": OLD_API,
        "target_api": TARGET_API,
        "old_symbols": ["../../etc/passwd"],
    }

    with _operator_approval([repository]):
        result = _call_over_stdio(
            {"repository": "approved-repo", "migration": hostile}
        )

    assert result.is_error is True
    text = result.content[0].text
    assert "dotted identifier" in text
    assert "passwd" not in text
