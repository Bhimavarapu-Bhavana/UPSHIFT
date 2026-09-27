# Phase 9 recovery layer

Phase 9 adds the UPSHIFT recovery layer. It sits on top of the existing Phase 4-8
components and answers one question:

> A migration failed independent verification. What now?

The layer exists to enforce a single invariant:

> **UPSHIFT never silently leaves a failed migration in an accepted state.**

## What this phase does not do

Phase 9 is recovery behavior only. It does not rewrite the Impact Analyzer,
Risk Analyzer, Verification Engine, Decision Engine, the MCP foundation, or the
IBM Bob integration. Recovery coordinates evidence those components already
produced; it never recomputes any of it. All eight protected Phase 4-7 files and
all Phase 8.1/8.2 MCP files are byte-identical before and after this phase.

## Flow

```text
  Phase 6  Verification            Phase 7  Decision
      |                               |
      v                               v
  candidate status  ------------>  ACCEPT / REJECT / INCONCLUSIVE
                                          |
                                          v
                             +--- Phase 9 recovery state machine ---+
                             |                                        |
                             v                                        v
                      recovery action                    structured re-plan proposal
                             |                                        |
                             v                                        v
              Phase 6 verification again                     (no repository change)
                             |
                             v
                     final outcome
```

## Recovery states

The recovery outcome is an explicit state, never a set of boolean flags. Flags
cannot express "rollback was attempted but produced no usable baseline" without
an ambiguous third meaning.

| State | Meaning |
| --- | --- |
| `ACCEPTED` | verification passed and the decision is ACCEPT |
| `ROLLBACK_REQUIRED` | verification failed; the migration cannot be accepted |
| `ROLLING_BACK` | a controlled baseline restoration is in progress |
| `ROLLBACK_VERIFICATION` | the restored baseline is being verified independently |
| `REPLANNING` | no verified baseline could be established; a proposal is being recorded |
| `INCONCLUSIVE` | recovery ended without acceptance and without a verified baseline |
| `RECOVERY_FAILED` | a verified baseline could not be established |
| `COMPLETED` | a verified final state was reached |

## State transitions

Normal success:

```text
verification PASS  -> decision ACCEPT  -> ACCEPTED  -> COMPLETED
```

Failed migration with a safe rollback:

```text
verification FAIL
  -> ROLLBACK_REQUIRED
  -> ROLLING_BACK
  -> ROLLBACK_VERIFICATION
  -> baseline verification PASS
  -> COMPLETED
```

Failed migration where the restored baseline does not verify:

```text
verification FAIL
  -> ROLLBACK_REQUIRED
  -> ROLLING_BACK
  -> ROLLBACK_VERIFICATION
  -> baseline verification FAIL
  -> RECOVERY_FAILED
```

Inconclusive migration:

```text
verification INCONCLUSIVE  -> INCONCLUSIVE
```

Rollback unavailable:

```text
verification FAIL
  -> ROLLBACK_REQUIRED
  -> REPLANNING
  -> INCONCLUSIVE
```

Every transition is recorded in the report with a reason, so the full path is
auditable after the fact.

### COMPLETED after recovery does not mean the migration succeeded

When recovery restores a verified baseline, the migration itself remains
rejected. The report records `initial_decision: REJECT` and `accepted: false`,
and the explanations include an explicit note that the migration must not be
reported as successful. `COMPLETED` means "the workspace is in a verified,
accounted-for state", not "the migration worked".

## Rollback abstraction

Restoration is delegated to an injected `RollbackProvider`. The abstraction
exposes exactly three operations:

```python
class RollbackProvider(Protocol):
    def rollback_available(self, candidate_id: str) -> bool: ...
    def restore_baseline(self, candidate_id: str) -> RollbackResult: ...
    def load_restored_baseline(self) -> VerificationCandidate: ...
```

This is deliberately not a generic "run this" interface. The only parameter
anywhere in the contract is an allowlisted `candidate_id`. There is no
`command`, `args`, `script`, `path`, or `callable` parameter, so no provider can
be handed a shell command, a Python snippet, or a filesystem path and have it
executed. `RollbackResult` is a frozen dataclass carrying only descriptive
fields: `attempted`, `succeeded`, `reason`, `restored_candidate_id`, `mechanism`.

The engine performs at most one restoration attempt and at most one independent
baseline verification. The recovery path is straight-line code with no loop, so
an autonomous retry cannot be introduced into it.

## Benchmark rollback mechanism

`app/verification/benchmark_rollback.py` provides the single concrete provider
used by the demonstration. Restoration is a **controlled restore into a
temporary working copy**:

1. The known-good OLD baseline package is **read** from
   `demo/migration_benchmark/old/`. Every `.py` file is copied, because the
   baseline service depends on its baseline directory module through a relative
   import.
2. The exact text is written into a fresh directory created by `tempfile.mkdtemp`
   **outside the repository**, materialized as a real package under a fixed
   module name so the baseline's own relative imports resolve.
3. The restored copy is imported in-process and handed to the existing Phase 6
   verifier as a narrow callable.
4. The temporary directory is removed by `close()`, including when recovery
   raises, because the provider is usable as a context manager.

Consequences:

- The protected OLD baseline is only ever opened for reading, so the
  demonstration cannot permanently alter it.
- The workspace location is chosen by the provider, never by the caller, and is
  always outside the repository.
- `shutil.rmtree` is called exactly once, and only on the provider's own
  workspace handle. This is cleanup of a temporary fixture the provider created,
  not arbitrary filesystem deletion; a test asserts the sole call site and that
  the argument is the provider's own attribute.
- `candidate_id` is validated against the statically known benchmark candidates.
  An unknown identifier, a path-like string, or a command-like string is refused.
- No Git operation, no force operation, no network access, no credentials.

## Running the demonstration

```bash
python -m demo.migration_benchmark.recovery_demo
```

```text
[old_baseline]         PASS: the known-good baseline passed 5 of 5 behavior case(s)
[correct_migration]    COMPLETED: decision ACCEPT on verification PASS; no rollback needed
[regression_migration] FAIL: decision REJECT; expected 'Amazing Grace' but observed 'Grace Hopper'
[restored_baseline]    PASS: user-002 now returns 'Amazing Grace'
[regression_migration] COMPLETED: ROLLBACK_REQUIRED -> ROLLING_BACK
                                 -> ROLLBACK_VERIFICATION -> COMPLETED
```

The demonstration is deterministic. It contains no randomness, no clock, and no
temporary path in its output: reports use the stable label
`temporary working copy/...` rather than the real temporary directory name, so
identical inputs always produce identical reports.

## Replanning is a proposal

When recovery cannot establish a verified baseline, the engine emits a
`ReplanRequest` containing:

- `migration_name`
- `candidate_id`
- `failed_verification_cases`
- `risk_evidence`
- `impact_evidence`
- `rollback_result`
- `baseline_verification_status`
- `recommended_investigation_areas`
- `status: "proposal"`

Producing a request performs no repository modification, schedules no attempt,
and grants no authority to retry. The `status` field is validated to remain
`"proposal"`, and the request is a frozen dataclass, so it cannot be promoted
into an action. A human or the orchestrating agent decides what to do with it.

There is no autonomous migration retry loop. Recovery stops at
`COMPLETED`, `RECOVERY_FAILED`, or `INCONCLUSIVE` and hands the outcome back.

## Recovery report

`RecoveryReport` is a frozen dataclass holding no engine, provider, or live
object, so a report cannot be used to reach back into the system and change an
outcome. It records:

| Field | Meaning |
| --- | --- |
| `migration_name` | the migration under recovery |
| `initial_candidate_id` | the candidate that was verified |
| `initial_decision` | the Phase 7 decision, carried in unchanged |
| `initial_verification_status` | verification status before recovery |
| `recovery_state` | the final recovery state |
| `rollback_attempted` | whether a restoration was attempted |
| `rollback_succeeded` | whether the restoration produced a usable baseline |
| `rollback_verification_status` | independent verification of the restored baseline, or `NOT_RUN` |
| `final_verification_status` | the verification status recovery ended on |
| `final_candidate_id` | the candidate in effect at the end |
| `replanning_required` | whether a re-plan proposal was produced |
| `explanations` | ordered, deterministic explanations |
| `evidence` | categorized evidence items |
| `transitions` | the full state path with a reason per step |
| `replan_request` | the proposal, when one was produced |

## Safety properties

The recovery engine:

- never silently approves; the only route to `ACCEPTED` requires an
  independently verified `PASS`
- never skips verification; a restored baseline must pass the same Phase 6
  verifier before recovery can report `COMPLETED`
- never treats risk as correctness; risk level is carried as context only
- never bypasses human approval; it grants no approval authority
- never executes arbitrary commands; the engine imports no execution primitive
- never accesses credentials or secrets
- never exposes a network service; the engine imports no networking module
- never modifies Git history and performs no force operation
- never creates an autonomous retry loop
- never creates an autonomous migration acceptance

### A failed verification cannot become ACCEPTED

`_guard_against_unsupported_acceptance` checks the evidence pair before any
recovery work starts. A `FAIL` status must pair with `REJECT`, `INCONCLUSIVE`
with `INCONCLUSIVE`, and only `PASS` may pair with `ACCEPT`. A report claiming
`ACCEPT` over failed verification is rejected as malformed evidence rather than
honored, so the engine has no code path that accepts an unverified migration.

`RecoveryReport.__post_init__` independently enforces that a recovered migration
may only be `COMPLETED` when the restored baseline was independently verified.

## Relationship to MCP and IBM Bob

Phase 9 adds no MCP tool. The single existing read-only `get_migration_context`
tool is unchanged, and rollback is deliberately **not** exposed over MCP. A test
asserts the MCP tool surface is still exactly `["get_migration_context"]` and
that the server module contains no rollback or recovery surface.

UPSHIFT recovery is independently testable without Bob, and no Phase 9 test
requires an IBM Bob runtime. The same Phase 8.2 limitation still stands: live
Bob UI verification is pending and remains a documentation item, not a reason to
redesign the MCP architecture.

## Tests

`tests/test_recovery_engine.py` covers all eighteen required areas: each
transition, both `RECOVERY_FAILED` variants, the `REPLANNING` and inconclusive
paths, risk independence, the acceptance guard, absence of arbitrary commands,
absence of repository mutation, determinism, immutability, proposal-only
replanning, absence of a retry loop, and the unchanged Phase 4-8 baseline.

`demo/migration_benchmark/tests/test_recovery_rollback.py` covers the
four-stage benchmark demonstration and asserts the protected baseline is never
modified and that the opt-in regression demonstration is not weakened.
