# Independent Verification Engine

Phase 6 adds an independent verification engine to UPSHIFT. It answers a different question than risk analysis:

> Does a migration candidate actually produce the behavior the benchmark requires?

## 1. Purpose

Risk analysis (Phase 5) estimates how much evidence of migration complexity exists. Verification (Phase 6) decides whether a candidate preserves externally visible behavior.

The engine compares candidate behavior against explicitly declared expected behavior, case by case, and preserves the evidence for every case. It never estimates failure probability and never repairs anything.

## 2. Independence from risk analysis

A migration is not considered verified merely because its risk score is low.

The verifier is independent by construction:

- `app/verification/verifier.py` does not import `app.core.risk_analyzer`, does not accept a `RiskReport`, and has no risk attribute.
- There is no threshold rule such as `if risk_score < X: safe`. Risk never determines correctness.
- `VerificationReport` contains no risk fields.
- Both directions are proven by tests: a `LOW`/`0` risk migration can still verify as `FAIL`, and a `HIGH` risk benchmark migration can verify as `PASS`.

`ImpactReport` → `RiskReport` → `VerificationReport` can coexist in one workflow; the reports are passed side by side, never merged.

## 3. Verification cases

A `VerificationCase` declares:

| Field | Meaning |
| --- | --- |
| `case_id` | Stable identifier, also used as the evidence label |
| `description` | Human-readable intent of the case |
| `inputs` | Arguments passed to the candidate's behavior callable |
| `expected` | Required behavior; `UNAVAILABLE` when the benchmark defines none |
| `required` | Required cases decide the overall status; optional cases are skipped |

For the profile-label migration, the required cases come from `demo/migration_benchmark/migration_metadata.json` (`preserved_behavior`), in metadata order:

| Case | Required label |
| --- | --- |
| `user-001` | `Ada Lovelace` |
| `user-002` | `Amazing Grace` |
| `user-003` | `Unknown` |
| `empty-user` | `Unnamed user` |
| `missing-user` | `Unknown user` |

No expected label is hard-coded in the generic engine. `app/verification/profile_label_benchmark.py` is the benchmark layer: it reads the expectations from the benchmark's own metadata and exposes the allowlisted benchmark candidates. A new migration adds its own benchmark metadata and adapter; the engine is reused unchanged.

## 4. Verification statuses

Case statuses are `PASS`, `FAIL`, `INCONCLUSIVE`, and `SKIPPED`. The report status is `PASS`, `FAIL`, or `INCONCLUSIVE`:

| Report status | Condition |
| --- | --- |
| `FAIL` | At least one required case failed, or a candidate raised while resolving a case |
| `INCONCLUSIVE` | No required case was evaluated, or a required case has no defined expected behavior |
| `PASS` | Every required case passed |

Missing evidence is never silently converted into `PASS`.

## 5. Evidence model

Every case produces a `VerificationResult` with `expected`, `observed`, `status`, and an `evidence` sentence, rendered by `format_case_evidence`:

```text
case: user-002
description: Profile label for user-002
expected: 'Amazing Grace'
observed: 'Grace Hopper'
status: FAIL
evidence: Behavior mismatch: expected 'Amazing Grace' but observed 'Grace Hopper'.
```

`VerificationReport` records the migration name, candidate identifier and path, total/passed/failed/inconclusive/skipped counts, ordered results, explanations, and `failure_evidence`.

Comparison is strict: `type(observed) is type(expected)` and equality must hold, so a wrong type can never pass as a match.

## 6. Regression detection

The `regression_migration` candidate migrates the call site and renamed fields but ignores the optional TARGET `display_name`. Verification catches it:

```text
candidate: regression_migration
status: FAIL
failed: 1 of 5 required cases
failed case: user-002
expected: 'Amazing Grace'
observed: 'Grace Hopper'
```

This is a controlled `FAIL` produced as data inside a `VerificationReport`. The pytest suite itself stays green, and the benchmark candidate is not modified.

## 7. Determinism

For the same candidate and case set, the engine returns an equal `VerificationReport`:

- fixed case order (declaration order, never sorted or shuffled by content);
- fixed result order and fixed explanation order;
- integer counts, plain string evidence, no timestamps in correctness;
- no randomness, no LLM calls, no network calls, no environment-dependent behavior;
- candidate behavior is read from the benchmark's fixed in-memory records.

## 8. Safety and read-only guarantees

The verifier does not:

- modify source, candidate, or benchmark files;
- apply patches or write any file;
- run Git or shell commands;
- access credentials or contact external services;
- invoke IBM Bob, MCP, rollback, or autonomous repair;
- accept an arbitrary module path, file path, or command from a caller.

The only executed code is the benchmark's own controlled verification code. `load_candidate` resolves identifiers through a fixed allowlist and raises `UnknownCandidateError` for anything else, so the public API cannot be used as a general-purpose code execution service. `VerificationCandidate.resolve` is a narrow in-process callable supplied by the trusted adapter. (Python may write its own `__pycache__` bytecode when a benchmark module is imported; that is interpreter behavior, not a verifier action.)

## 9. Current benchmark example

```python
from app.verification.profile_label_benchmark import verify_candidate

correct = verify_candidate("correct_migration")
assert correct.status == "PASS"          # 5/5 required cases pass

regression = verify_candidate("regression_migration")
assert regression.status == "FAIL"       # user-002 expected 'Amazing Grace', observed 'Grace Hopper'
```

`verify_candidate("old_baseline")` verifies the OLD baseline against the same cases and passes, confirming the declared expectations match pre-migration behavior.

## 10. Current limitations

Verification currently proves behavior against the controlled benchmark; it is not yet a general production-code semantic verifier. It does not implement:

- static or dynamic analysis of arbitrary application code;
- coverage of behavior outside the declared cases;
- performance, security, or concurrency properties;
- migration execution, planning, or repair;
- post-migration verification beyond the declared cases;
- rollback or recovery.

Verification proves only that the declared cases produced the declared behavior for the given candidate.

## 11. Future integration with Bob

IBM Bob remains the agentic coding and orchestration layer. A future workflow will let Bob produce a candidate and then run this engine on the result, keeping the ordering constraint intact: Bob may propose work, but it may not mark a migration successful. Only a `PASS` verification report may satisfy the verification gate, a `FAIL` report must be surfaced as evidence, and an `INCONCLUSIVE` report must never be treated as approval. No Bob, MCP, or orchestration wiring is implemented in this phase.

## Files

- `app/verification/verifier.py`: benchmark-agnostic engine (data model, `CandidateVerifier`, statuses, evidence rendering).
- `app/verification/profile_label_benchmark.py`: profile-label benchmark layer (metadata-driven cases, allowlisted candidate loading).
- `tests/test_verifier.py`: focused verification, regression, determinism, safety, and independence tests.
