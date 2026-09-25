# Risk Analysis Engine

The UPSHIFT risk analyzer converts existing Phase 4 impact evidence into a deterministic, explainable migration-complexity assessment. It answers:

> How risky is this proposed migration based on the evidence currently available?

Risk analysis estimates migration complexity from observable repository evidence. It is not a probability of failure.

## Purpose

The engine provides a small prototype risk model for the controlled migration benchmark. It does not execute the migration, repair code, detect regressions, or verify the result. Those are future UPSHIFT phases.

## Inputs

`RiskAnalyzer` consumes:

- `MigrationDescription`: the migration identity, OLD/TARGET API, renamed pairs, and metadata-derived file hints.
- `ImpactReport`: the read-only evidence produced by `ImpactAnalyzer`.

The intended flow is:

```python
impact_report = ImpactAnalyzer(repository_root).analyze(migration)
risk_report = RiskAnalyzer().analyze(migration, impact_report)
```

The risk engine does not independently rescan the repository. This keeps the impact algorithm single-sourced and makes the risk calculation reproducible from the same report.

## Risk factors

The model emits up to five evidence-based factors in a fixed order:

1. `direct_old_api_usage` — one point of evidence for each direct OLD API reference, weighted at 2 points.
2. `renamed_symbols` — one point of evidence for each renamed field or symbol pair, weighted at 3 points.
3. `test_impact` — one point of evidence for each affected test file, weighted at 2 points.
4. `multiple_impacted_files` — coordination evidence for every impacted file after the first, weighted at 1 point per extra file.
5. `mixed_api_state` — explicit evidence when both OLD and TARGET references exist, weighted at 2 points.

A factor is present only when its evidence condition is true. Mixed OLD/TARGET references are reported as a compatibility state; they are never labeled an automatic regression.

## Scoring formula

The deterministic score is:

```text
risk_score =
    (direct_old_reference_count * 2)
  + (renamed_symbol_count * 3)
  + (affected_test_file_count * 2)
  + max(0, impacted_file_count - 1)
  + (2 if direct_old_reference_count > 0 and target_reference_count > 0 else 0)
```

The score is a transparent evidence weight, not a statistical estimate, probability, or machine-learning output.

## Risk thresholds

The score maps to three levels:

| Score | Level |
| --- | --- |
| `0`–`5` | `LOW` |
| `6`–`11` | `MEDIUM` |
| `12` or higher | `HIGH` |

The same thresholds are used for the severity of an individual factor's contribution.

## Output and explainability

`RiskReport` contains:

- Migration name and API endpoints.
- Overall `risk_level` and integer `risk_score`.
- Ordered `RiskFactor` entries.
- Impacted file count.
- Direct OLD reference count.
- TARGET reference count.
- Metadata-listed file count.
- Affected test files.
- Renamed symbol count.
- Generated `explanations`.

Each factor records its category, severity, score, evidence string, human-readable reason, and affected paths/symbols when available. Explanations are generated from the actual counts in the input report. With no evidence, the report is `LOW` with a clear no-evidence explanation.

## Determinism

Risk analysis uses no randomness, timestamps, network calls, LLM calls, environment values, or repository mutation. Factor order, symbol order, path order, arithmetic, and thresholds are fixed. Re-analyzing the same migration and `ImpactReport` produces an equal `RiskReport`.

## Safety and read-only guarantees

`RiskAnalyzer` operates on already-produced dataclasses and does not perform filesystem operations. It does not:

- read or write repository files;
- execute migrations or arbitrary repository code;
- invoke shell commands or Git;
- access credentials or external services;
- create patches or modify the benchmark;
- perform rollback or recovery.

Repository scanning and path-safety enforcement remain the responsibility of Phase 4.

## Current benchmark example

For the profile-label migration, the report may show factors such as direct OLD references in `demo/migration_benchmark/old/profile_service.py`, renamed-field evidence from the metadata, and affected tests under `demo/migration_benchmark/tests/`. The exact counts come from the current `ImpactReport`; no benchmark result is hard-coded in the engine.

## Current limitations

This model does not yet implement:

- semantic correctness verification;
- regression detection;
- post-migration verification;
- migration execution or repair;
- rollback or recovery;
- IBM Bob integration;
- MCP;
- dashboards or cloud services.

Affected test files are identified from metadata-listed and impacted paths using deterministic test-path conventions. The model is a focused prototype and should be extended only with clearly defined, evidence-based factors.
