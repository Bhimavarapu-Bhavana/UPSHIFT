"""Tests proving UPSHIFT reads a real repository's contents dynamically.

Why this module exists separately from ``tests/test_repository_input.py``
--------------------------------------------------------------------------

That module holds the *boundary* of real-repository mode: the allowlist, the
traversal and credential refusals, the budgets, the HTTP shapes, and the proof
that the controlled benchmark is untouched. It asserts those properties using a
small fixed fixture.

This module answers a different and narrower question: **does UPSHIFT actually
read the bytes that are on disk, or does it reproduce a remembered answer?** A
migration-safety tool that reported a confident impact summary it had not
actually derived would be worse than useless, so that question is pinned here
directly.

How the proof is built
----------------------

Every expectation in this module is *derived from content the test itself
writes*, never copied from a fixture and never hardcoded into the product:

* a symbol is planted at a known line, and that line is recomputed from the
  bytes before the assertion, so a stale or memorized line number fails;
* the same declaration is run against two different repositories with different
  symbols, and each must report only its own;
* a repository is analyzed, mutated, and analyzed again, and the two results
  must differ in the specific way the mutation predicts.

If the product stopped reading the repository, returned a constant, or shipped a
pre-baked analysis for a sample tree, every test in sections B and C fails. No
test here reads a real user's source tree; all fixtures are built in
``tmp_path``, and files are written with ``write_bytes`` so the exact bytes -
including line endings - are controlled by the test rather than by the
platform's newline translation.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Union

import pytest

from app.api.repository_service import analyze_repository
from app.repository.real_input import RealRepositoryInput
from app.repository.snapshot import bounded_snapshot

#: Bytes or text for a fixture file. Text is encoded as UTF-8 by the helper.
Content = Union[str, bytes]


def build_repository(root: Path, files: Mapping[str, Content]) -> Path:
    """Create a repository tree with exactly the files a test asks for.

    Bytes are written verbatim with ``write_bytes`` so that line endings are the
    test's decision, not the platform's. That matters because a reader that
    round-trips content incorrectly would still look correct against
    platform-normalized fixtures.
    """

    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return root


def migration_for(package: str) -> Dict[str, object]:
    """A declaration naming one package's entry point.

    The frozen Phase 4 engine derives the searched symbols from the entry
    point, so declaring ``alpha_pkg.old_call`` is what makes the analyzer look
    for ``alpha_pkg`` and ``old_call``. Nothing about a repository is implied by
    the declaration, which is what these tests rely on.
    """

    return {
        "name": f"{package} migration",
        "old_api": f"{package}_pkg.old_call",
        "target_api": f"{package}_modern.new_call",
    }


def analyze(root: Path, declaration: Mapping[str, object], boundary: Path) -> Dict[str, object]:
    """Run the real-repository pipeline over one temporary repository."""

    return analyze_repository(str(root), dict(declaration), [str(boundary)]).to_dict()


def references(result: Mapping[str, object]) -> List[str]:
    """The rendered direct references, as the dashboard displays them."""

    return list(result["impact"]["direct_references"])  # type: ignore[index]


def line_of(body: bytes, needle: str) -> int:
    """Recompute the 1-based line a string occupies, from the bytes written.

    Derived from the content rather than hardcoded, so a reader that reported a
    line number it had not actually counted fails the assertion.
    """

    for number, line in enumerate(body.decode("utf-8").splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not present in the fixture content")


# ---------------------------------------------------------------------------
# A. A valid repository is analyzed, and real files are read at runtime
# ---------------------------------------------------------------------------


def test_a_valid_temporary_repository_is_analyzed(tmp_path: Path) -> None:
    root = build_repository(tmp_path / "billing_service", {"svc/api.py": "x = 1\n"})

    result = analyze(root, migration_for("alpha"), tmp_path)

    assert result["mode"] == "real_repository"
    assert result["repository"]["repository_name"] == "billing_service"
    assert result["repository"]["inspected_paths"] == ["svc/api.py"]


def test_the_inspected_file_list_is_the_repository_that_was_written(tmp_path: Path) -> None:
    files = {
        "README.md": "# alpha\n",
        "pkg/__init__.py": "",
        "pkg/service.py": "def run():\n    return None\n",
        "pkg/deep/nested/helper.py": "VALUE = 1\n",
        "config/settings.yaml": "enabled: true\n",
    }
    root = build_repository(tmp_path / "tree_repo", files)

    result = analyze(root, migration_for("alpha"), tmp_path)

    assert result["repository"]["inspected_paths"] == sorted(files)
    assert result["repository"]["total_bytes_read"] == sum(
        len(text.encode("utf-8")) for text in files.values()
    )


# ---------------------------------------------------------------------------
# B. A symbol planted in a file is discovered, at the line it is really on
# ---------------------------------------------------------------------------


def test_a_symbol_written_into_a_file_is_discovered_at_its_real_line(
    tmp_path: Path,
) -> None:
    body = b"import os\n\n\ndef run(order):\n    return alpha_pkg.old_call(order.id)\n"
    root = build_repository(tmp_path / "planted", {"svc/api.py": body})

    result = analyze(root, migration_for("alpha"), tmp_path)

    expected = line_of(body, "alpha_pkg.old_call")
    assert expected == 5, "the fixture must place the symbol on a known line"
    assert "svc/api.py:5 alpha_pkg" in references(result)
    assert "svc/api.py:5 old_call" in references(result)
    assert "svc/api.py" in result["impact"]["impacted_files"]


def test_each_repository_reports_only_the_symbols_it_actually_contains(
    tmp_path: Path,
) -> None:
    """The anti-hardcoding proof: same declaration, two repositories, two answers."""

    alpha = build_repository(
        tmp_path / "alpha_service", {"svc/api.py": "x = alpha_pkg.old_call(1)\n"}
    )
    beta = build_repository(
        tmp_path / "beta_service", {"svc/api.py": "x = beta_pkg.old_call(1)\n"}
    )

    alpha_result = analyze(alpha, migration_for("alpha"), tmp_path)
    beta_result = analyze(beta, migration_for("beta"), tmp_path)

    assert "svc/api.py:1 alpha_pkg" in references(alpha_result)
    assert "svc/api.py:1 beta_pkg" in references(beta_result)
    assert not any("beta_pkg" in entry for entry in references(alpha_result))
    assert not any("alpha_pkg" in entry for entry in references(beta_result))
    assert alpha_result["impact"] != beta_result["impact"]


def test_a_symbol_absent_from_the_files_is_not_reported(tmp_path: Path) -> None:
    """A declared symbol is searched for, never assumed to be present."""

    root = build_repository(
        tmp_path / "unrelated", {"svc/api.py": "value = other_pkg.unrelated_call(1)\n"}
    )

    result = analyze(root, migration_for("alpha"), tmp_path)

    assert result["impact"]["direct_reference_count"] == 0
    assert result["impact"]["impacted_file_count"] == 0


def test_a_reference_in_a_second_file_is_found_too(tmp_path: Path) -> None:
    root = build_repository(
        tmp_path / "spread",
        {
            "svc/api.py": "x = alpha_pkg.old_call(1)\n",
            "svc/worker.py": "y = alpha_pkg.old_call(2)\n",
            "README.md": "docs mention alpha_pkg.old_call\n",
        },
    )

    result = analyze(root, migration_for("alpha"), tmp_path)

    assert sorted(result["impact"]["impacted_files"]) == [
        "README.md",
        "svc/api.py",
        "svc/worker.py",
    ]
    assert "svc/worker.py:1 alpha_pkg" in references(result)
    assert "README.md:1 alpha_pkg" in references(result)


def test_a_windows_line_ending_file_reports_its_true_line_numbers(
    tmp_path: Path,
) -> None:
    """Regression: the snapshot bridge must not re-wrap CRLF content.

    Copying CRLF content with platform newline translation stores CR CR LF. The
    analyzer's universal-newline read then sees a blank line at every original
    break, so every reported line number after the first is shifted and every
    line count is inflated. The evidence would describe UPSHIFT's scratch copy
    rather than the repository.
    """

    body = b"import os\r\n\r\n\r\ndef run(order):\r\n    return alpha_pkg.old_call(order.id)\r\n"
    root = build_repository(tmp_path / "crlf_repo", {"svc/api.py": body})

    result = analyze(root, migration_for("alpha"), tmp_path)

    expected = line_of(body, "alpha_pkg.old_call")
    assert expected == 5
    assert "svc/api.py:5 alpha_pkg" in references(result)
    assert "svc/api.py:5 old_call" in references(result)
    assert not any(entry.startswith("svc/api.py:7") for entry in references(result))


def test_a_snapshot_stores_the_exact_bytes_it_was_given() -> None:
    text = "alpha = 1\r\nbeta = 2\r\ngamma = 3\n"

    with bounded_snapshot([("pkg/mod.py", text)]) as snapshot_root:
        stored = (snapshot_root / "pkg" / "mod.py").read_bytes()

    assert stored == text.encode("utf-8")


# ---------------------------------------------------------------------------
# C. Changing the repository changes the result
# ---------------------------------------------------------------------------


def test_adding_a_call_site_changes_the_reported_result(tmp_path: Path) -> None:
    root = build_repository(
        tmp_path / "growing", {"svc/api.py": "first = alpha_pkg.old_call(1)\n"}
    )
    declaration = migration_for("alpha")

    before = analyze(root, declaration, tmp_path)
    assert before["impact"]["direct_reference_count"] == 2

    grown = b"first = alpha_pkg.old_call(1)\nsecond = alpha_pkg.old_call(2)\n"
    (root / "svc" / "api.py").write_bytes(grown)
    after = analyze(root, declaration, tmp_path)

    assert after["impact"]["direct_reference_count"] == 4
    assert f"svc/api.py:{line_of(grown, 'second')} alpha_pkg" in references(after)
    assert after != before


def test_removing_the_symbol_empties_the_impact_but_not_the_report(
    tmp_path: Path,
) -> None:
    root = build_repository(
        tmp_path / "shrinking", {"svc/api.py": "value = alpha_pkg.old_call(1)\n"}
    )
    declaration = migration_for("alpha")
    before = analyze(root, declaration, tmp_path)
    assert before["impact"]["impacted_file_count"] == 1

    (root / "svc" / "api.py").write_bytes(b"value = 1\n")
    after = analyze(root, declaration, tmp_path)

    assert after["impact"]["direct_reference_count"] == 0
    assert after["impact"]["impacted_file_count"] == 0
    assert after != before
    # The honest verdict is unchanged: a repository UPSHIFT only reads is never
    # accepted, whether or not anything was found in it.
    assert after["mode"] == "real_repository"
    assert after["decision"]["decision"] == "INCONCLUSIVE"
    assert after["final_outcome"] == "MIGRATION_INCONCLUSIVE"
    assert after["decision"]["accepted"] is False


def test_a_file_added_to_the_repository_is_picked_up(tmp_path: Path) -> None:
    root = build_repository(tmp_path / "expanding", {"svc/api.py": "value = 1\n"})
    declaration = migration_for("alpha")
    before = analyze(root, declaration, tmp_path)
    assert before["impact"]["impacted_file_count"] == 0

    build_repository(root, {"extra/worker.py": "y = alpha_pkg.old_call(2)\n"})
    after = analyze(root, declaration, tmp_path)

    assert after["impact"]["impacted_files"] == ["extra/worker.py"]
    assert "extra/worker.py" in after["repository"]["inspected_paths"]
    assert after != before


def test_a_deleted_file_leaves_the_evidence(tmp_path: Path) -> None:
    root = build_repository(
        tmp_path / "shrinking_tree",
        {"svc/api.py": "value = 1\n", "extra/worker.py": "y = alpha_pkg.old_call(2)\n"},
    )
    declaration = migration_for("alpha")
    before = analyze(root, declaration, tmp_path)
    assert before["impact"]["impacted_files"] == ["extra/worker.py"]

    (root / "extra" / "worker.py").unlink()
    after = analyze(root, declaration, tmp_path)

    assert after["impact"]["impacted_files"] == []
    assert after["repository"]["inspected_paths"] == ["svc/api.py"]
    assert after != before


def test_the_recorded_byte_total_tracks_the_content_on_disk(tmp_path: Path) -> None:
    root = build_repository(tmp_path / "sized", {"svc/api.py": "x = 1\n"})
    declaration = migration_for("alpha")
    before = analyze(root, declaration, tmp_path)

    padded = b"x = 1\n" + b"# padding\n" * 500
    (root / "svc" / "api.py").write_bytes(padded)
    after = analyze(root, declaration, tmp_path)

    assert after["repository"]["total_bytes_read"] == len(padded)
    assert after["repository"]["total_bytes_read"] > before["repository"]["total_bytes_read"]


def test_the_verification_cases_follow_the_discovered_symbols(tmp_path: Path) -> None:
    """The discovered references, not a fixed list, drive the reported cases."""

    declaration = migration_for("alpha")
    empty = analyze(
        build_repository(tmp_path / "quiet", {"svc/api.py": "value = 1\n"}),
        declaration,
        tmp_path,
    )
    populated = analyze(
        build_repository(tmp_path / "loud", {"svc/api.py": "x = alpha_pkg.old_call(1)\n"}),
        declaration,
        tmp_path,
    )

    quiet_cases = [entry["case_id"] for entry in empty["verification"]["case_results"]]
    loud_cases = [entry["case_id"] for entry in populated["verification"]["case_results"]]

    assert quiet_cases == ["static_review:no_symbol_reference_discovered"]
    assert loud_cases == ["static_review:alpha_pkg", "static_review:old_call"]
    assert loud_cases != quiet_cases
    # No case carries an expectation, so none can invoke a resolver.
    assert all(
        entry["expected"] == "UNAVAILABLE"
        for entry in populated["verification"]["case_results"]
    )


def test_renaming_a_symbol_in_the_declaration_changes_which_references_appear(
    tmp_path: Path,
) -> None:
    """The declaration narrows the search; it does not create references."""

    body = b"x = alpha_pkg.old_call(1)\n"
    root = build_repository(tmp_path / "narrowed", {"svc/api.py": body})

    matching = analyze(root, migration_for("alpha"), tmp_path)
    other = analyze(
        root,
        {
            "name": "other migration",
            "old_api": "unrelated_pkg.some_call",
            "target_api": "unrelated_modern.new_call",
        },
        tmp_path,
    )

    assert matching["impact"]["direct_reference_count"] == 2
    assert other["impact"]["direct_reference_count"] == 0
    assert matching["impact"] != other["impact"]


# ---------------------------------------------------------------------------
# D. Regression: a root in an equivalent but unresolved form reads identically
# ---------------------------------------------------------------------------


def test_the_reader_normalizes_the_root_it_was_given(tmp_path: Path) -> None:
    """Containment compares resolved paths, so the root must be resolved too."""

    root = build_repository(tmp_path / "normalized", {"svc/api.py": "x = 1\n"})

    assert RealRepositoryInput(root).repository_root == root.resolve()


def test_an_equivalent_root_form_reads_the_same_repository() -> None:
    """Regression: an unresolved root used to produce a silent, empty read.

    ``tempfile`` can hand back a short form such as ``C:\\Users\\BHIMAV~1\\...``
    while every directory entry resolves to the long form. Comparing the two
    directly failed containment for *every* entry, so the read completed
    successfully having inspected nothing - indistinguishable, in the evidence,
    from a repository that genuinely contains no relevant file. The boundary
    always supplies a resolved root, so this was latent rather than reachable
    from the dashboard, but a wrong answer is a wrong answer.
    """

    created = Path(tempfile.mkdtemp(prefix="upshift-root-form-"))
    try:
        build_repository(created, {"pkg/api.py": "x = 1\n"})
        unresolved = Path(str(created))
        if str(unresolved) == str(unresolved.resolve()):
            pytest.skip("this platform does not distinguish the two path forms")

        from_unresolved = RealRepositoryInput(unresolved).load()
        from_resolved = RealRepositoryInput(unresolved.resolve()).load()

        assert from_unresolved.evidence.inspected_file_count == 1
        assert from_unresolved.files == from_resolved.files
        assert from_unresolved.evidence == from_resolved.evidence
    finally:
        shutil.rmtree(created, ignore_errors=True)


def test_reading_an_equivalent_root_form_never_reports_a_boundary_escape() -> None:
    created = Path(tempfile.mkdtemp(prefix="upshift-root-form-"))
    try:
        build_repository(created, {"pkg/api.py": "x = 1\n"})
        unresolved = Path(str(created))
        if str(unresolved) == str(unresolved.resolve()):
            pytest.skip("this platform does not distinguish the two path forms")

        evidence = RealRepositoryInput(unresolved).load().evidence

        assert evidence.skipped_file_count == 0
        assert "outside_repository_boundary" not in {
            summary.reason for summary in evidence.skipped
        }
    finally:
        shutil.rmtree(created, ignore_errors=True)
