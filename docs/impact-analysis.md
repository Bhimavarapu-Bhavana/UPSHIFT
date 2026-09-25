# Impact Analysis Engine

The UPSHIFT impact analyzer is a small, deterministic, read-only component that identifies repository files which may be affected by a described API migration. It is the first focused intelligence layer above the controlled migration benchmark.

This component is not a general-purpose static-analysis framework. It performs bounded textual reference detection for the migration symbols supplied to it and combines that evidence with explicit migration-metadata hints.

## Purpose

The analyzer answers one question: which files inside the supplied UPSHIFT repository are potentially impacted by this migration?

It reports evidence rather than a safety judgment. A detected reference is not a claim that the file is broken or unsafe.

## Input contract

`ImpactAnalyzer` is constructed with one repository root:

```python
from app.core.impact_analyzer import ImpactAnalyzer, MigrationDescription
```

`MigrationDescription` contains:

- `name`: migration identifier.
- `old_api` and `target_api`: human-readable API endpoints.
- `old_symbols`: textual OLD identifiers to search for.
- `target_symbols`: textual TARGET identifiers to search for.
- `renamed_symbols`: `(old, target)` pairs used to explain renamed-field evidence.
- `affected_files`: optional repository-relative files supplied by migration metadata.

The benchmark metadata can be adapted without duplicating its contract:

```python
import json
from pathlib import Path

metadata = json.loads(
    Path("demo/migration_benchmark/migration_metadata.json").read_text(encoding="utf-8")
)
migration = MigrationDescription.from_metadata(metadata)
report = ImpactAnalyzer(".").analyze(migration)
```

`from_metadata()` extracts the OLD and TARGET entry points, field lists, and affected files from the benchmark metadata shape.

## Output contract

`analyze()` returns a frozen `ImpactReport` containing:

- `migration_name`, `old_api`, and `target_api`.
- Normalized OLD and TARGET symbol lists and renamed pairs.
- `impacted_files`: deterministic `FileImpact` entries sorted by repository-relative path.
- `direct_references`: line and column locations for OLD references.
- `target_references`: line and column locations for TARGET references.
- `metadata_listed_files`: normalized files explicitly named by migration metadata.

Each `FileImpact` contains its path, human-readable reasons, whether it was metadata-listed, and the OLD/TARGET symbols found. Reasons use language such as `potentially impacted` and `reference detected`; they do not assert that a file is definitely incorrect.

## Detection approach

The scanner:

1. Walks only the supplied repository root.
2. Sorts directory and file traversal to make results stable.
3. Skips `.git`, `.venv`, `__pycache__`, `.pytest_cache`, build/dist directories, and other generated or local-tool caches.
4. Skips `.env` files and the `secrets/` directory.
5. Reads a bounded set of text formats, including Python, Markdown, JSON, and configuration files.
6. Matches configured symbols as identifier-like textual tokens, not arbitrary substrings.
7. Adds metadata-listed files even when no direct symbol match exists.
8. Sorts impacted files, references, symbols, and reasons deterministically.

The analyzer does not parse or execute application code. A match in a comment, documentation file, or metadata file is still reported as evidence because this phase is intentionally conservative and textual.

## Safety restrictions

The analyzer is read-only and does not:

- edit, create, delete, or move files;
- execute repository code or discovered commands;
- invoke subprocesses or external services;
- read outside the supplied repository root;
- follow a supplied metadata path that resolves outside that root;
- inspect excluded secret or environment files.

A path that escapes the root raises `UnsafeRepositoryPathError`.

## Determinism

The result is independent of filesystem traversal order. Paths, references, symbol sets, and reasons are sorted before being returned. Re-running the analyzer against unchanged files produces an equal report.

## Current benchmark example

For the profile-label benchmark, the OLD symbols include `LegacyDirectory`, `lookup`, `first_name`, `last_name`, and `preferred_name`. The TARGET symbols include `ProfileDirectory`, `get_profile`, `given_name`, `family_name`, and `display_name`.

A report for the repository therefore provides evidence such as:

- `demo/migration_benchmark/old/profile_service.py` — OLD references and renamed-field evidence.
- `demo/migration_benchmark/tests/test_old_baseline.py` — OLD behavior and API references.
- `demo/migration_benchmark/target/profile_service.py` — existing TARGET API references.
- Migration metadata files — explicit impact hints.

These are potentially impacted files, not risk scores or proof of failure.

## Limitations

This phase does not implement:

- risk scoring;
- regression detection;
- compatibility scoring;
- semantic correctness verification;
- migration execution or repair;
- rollback or recovery;
- IBM Bob integration;
- MCP;
- a dashboard.

The scanner does not understand dynamic imports, reflection, runtime configuration, generated code, or the full semantics of a symbol. Later phases may consume this evidence for deeper analysis, but this component intentionally remains small and deterministic.
