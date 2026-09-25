# Migration Decision Engine

Phase 7 adds the decision layer. It combines the three reports that already exist and answers one question:

> Given the migration evidence currently available, what decision is justified?

## 1. Purpose

| Phase | Question answered | Report |
| --- | --- | --- |
| 4 — Impact analysis | What could this migration touch? | `ImpactReport` |
| 5 — Risk analysis | How much complexity evidence exists? | `RiskReport` |
| 6 — Verification | Does the candidate preserve declared behavior? | `VerificationReport` |
| 7 — Decision | What is justified by that evidence? | `DecisionReport` |

The engine is a pure decision layer. It does not rescan the repository, recompute impact or risk, run verification, or execute anything.

## 2. Inputs

`MigrationDecisionEngine.decide(impact_report, risk_report, verification_report)` consumes three already-produced reports.

The engine deliberately avoids importing the Phase 4, 5, and 6 modules. It depends on the small attribute interface those reports expose and validates that interface at runtime:

| Report | Attributes the engine requires |
| --- | --- |
| Impact | `migration_name`, `impacted_files`, `direct_references`, `target_references` |
| Risk | `migration_name`, `risk_level`, `risk_score` |
| Verification | `migration_name`, `candidate_id`, `status`, `total_cases`, `passed_cases`, `failed_cases`, `inconclusive_cases`, `skipped_cases`, `results` |

A missing attribute, a non-integer or negative count, an unknown verification status, or reports that describe different migrations raises `DecisionInputError` instead of producing a decision from malformed evidence.

## 3. Decision states

`ACCEPT`, `REJECT`, `INCONCLUSIVE`. No other value is valid, and `DecisionReport` rejects anything else at construction.

## 4. ACCEPT rule

`ACCEPT` only when verification status is `PASS` **and** at least one required case was evaluated and every evaluated required case passed.

`HIGH` risk with a passing verification is `ACCEPT`. Risk is never an independent blocker.

## 5. REJECT rule

`REJECT` when verification status is `FAIL`, or when any required case failed. A failed verification is sufficient evidence that the candidate did not preserve declared behavior.

A failed case also takes precedence if a report is internally inconsistent, for example a `PASS` status that still carries failed cases. The layer fails closed.

## 6. INCONCLUSIVE rule

`INCONCLUSIVE` when required evidence is insufficient:

- no required case was evaluated (for example, zero cases);
- every available case was optional and skipped;
- a required case has no defined expected behavior;
- verification status is itself `INCONCLUSIVE`;
- verification claims `PASS` but fewer cases passed than were required.

`INCONCLUSIVE` never becomes `ACCEPT`.

## 7. Why risk does not determine correctness

> Risk analysis describes migration complexity evidence; verification establishes behavioral correctness.

The engine contains no rule of the form `if risk_level == "HIGH": REJECT`, `if risk_score > X: REJECT`, or `if risk_score < X: ACCEPT`. Risk level and score are copied into the report as context only, and the `risk` evidence entry states that it does not determine correctness.

Both directions are proven by tests: a `LOW`/`0` risk migration with failing verification is `REJECT`, and the `HIGH` risk profile-label benchmark with a correct candidate is `ACCEPT`.

## 8. Evidence and explanations

> A migration is accepted only when independent verification establishes that the declared required behavior is preserved.

`DecisionReport` is a frozen dataclass holding decision-relevant fields only: migration name, decision, verification status, risk level, risk score, case counts, candidate id, ordered `explanations`, and ordered `evidence`.

`evidence` holds three `DecisionEvidence` entries in fixed order:

| Kind | Content |
| --- | --- |
| `verification` | Case counts, with one detail line per failed case: `Case user-002 failed: expected 'Amazing Grace', observed 'Grace Hopper'.` |
| `risk` | `Risk level: HIGH with score 241 (complexity evidence only; it does not determine correctness).` |
| `impact` | Impacted file and reference counts |

`explanations` is a deterministic tuple: the decision statement, the independence statement, the verification counts, the risk context line, then per-case failure evidence. Risk never appears as a cause.

## 9. Benchmark examples

```text
old_baseline          verification PASS  -> decision ACCEPT
correct_migration     verification PASS  -> decision ACCEPT
regression_migration  verification FAIL  -> decision REJECT
```

Both candidates share the same `ImpactReport` and `RiskReport` for the profile-label migration, yet they receive different decisions. That is the concrete demonstration that risk != correctness.

## 10. Determinism

Identical reports always produce an equal `DecisionReport`. There are no timestamps, no randomness, no environment reads, no filesystem scans, no subprocesses, no network calls, and no LLM calls. Evidence and explanation ordering is fixed by construction.

## 11. Safety and read-only guarantees

The decision engine operates only on report objects that already exist. It does not modify source, candidate, or benchmark files; create patches; execute migrations; run shell or Git commands; call external services; invoke IBM Bob or MCP; perform rollback or recovery; perform repair; or accept arbitrary commands.

Its only imports are from the standard library (`dataclasses`, `typing`).

## 12. Current limitations

- It does not execute, apply, plan, or repair a migration.
- It cannot verify anything on its own; it only interprets a `VerificationReport` produced elsewhere.
- It has no notion of approval thresholds, reviewer sign-off, or staged rollout.
- It does not consider test-suite health, environment drift, or non-behavioral risk such as performance or security.
- It does not persist, sign, or transmit decisions.
- Mixed evidence is rejected as invalid input rather than partially interpreted.

## 13. Future IBM Bob integration

IBM Bob remains the agentic coding and orchestration layer. A future integration would let Bob propose or apply a candidate and then request a decision, with two boundaries that this phase already establishes: Bob may never mark a migration successful on its own, only a `PASS` verification-backed `ACCEPT` satisfies the verification gate, and a `REJECT` or `INCONCLUSIVE` decision must be surfaced rather than retried into acceptance. No Bob, MCP, execution, or rollback wiring is implemented in this phase.

## Files

- `app/core/decision_engine.py`: `MigrationDecisionEngine`, `DecisionReport`, `DecisionEvidence`, decision states, and input validation.
- `tests/test_decision_engine.py`: 32 focused decision, independence, determinism, safety, and integration tests.
