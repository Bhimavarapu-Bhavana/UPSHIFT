# Profile Label Directory Migration Benchmark

This benchmark is a small, deterministic repository for demonstrating a controlled software migration. It is the application that a future UPSHIFT workflow can analyze, migrate through IBM Bob, verify, and potentially roll back. It is not the UPSHIFT intelligence engine.

All records are fixed in memory. The benchmark has no network, database, cloud, credential, or external service dependency.

## Scenario

The application renders a human-readable profile label from a directory lookup. The OLD application uses a legacy API. The TARGET contract changes the lookup method and all profile field names while preserving the application’s externally visible label behavior.

This creates several migration concerns that are easy to inspect:

- A renamed dependency method.
- Renamed profile fields.
- An optional preferred-name value whose semantics must be preserved.
- Missing and empty profile cases that are easy to mishandle.
- Behavior tests that can detect an incorrect migration.

## OLD contract

The OLD implementation is in `demo/migration_benchmark/old/`.

- `LegacyDirectory.lookup(user_id)` returns a `LegacyProfile` or `None`.
- `LegacyProfile` exposes `first_name`, `last_name`, and optional `preferred_name`.
- A non-empty `preferred_name` wins.
- Otherwise, non-empty name parts are joined with one space.
- A missing profile returns `Unknown user`.
- A profile with no usable name parts returns `Unnamed user`.

## TARGET contract

The TARGET reference fixture is in `demo/migration_benchmark/target/`.

- `ProfileDirectory.get_profile(user_id)` returns a `Profile` or `None`.
- `Profile` exposes `given_name`, `family_name`, and optional `display_name`.
- The target `display_name` has the same semantic role as the OLD optional `preferred_name`; it is not guaranteed to contain a full name.
- The target service must preserve the OLD label rules and fallback values.

The `target/` package is a reference fixture that makes the new contract visible. It is not a migration runner and does not change the OLD baseline.

## Files involved

- `old/legacy_directory.py`: OLD dependency contract and fixed records.
- `old/profile_service.py`: OLD application behavior that consumes the dependency.
- `target/profile_directory.py`: TARGET dependency contract and equivalent fixed records.
- `target/profile_service.py`: Reference implementation of the migrated behavior.
- `tests/test_old_baseline.py`: Baseline behavior and OLD contract checks.
- `tests/test_target_fixture.py`: Checks for the TARGET reference fixture.
- `migration_metadata.json`: Machine-readable contract, migration, risk, and verification notes.

## Expected migration

A future migration should update the OLD service call site to the TARGET API, adapt the renamed fields, and retain the existing behavior rules. The expected behavior cases are recorded in `migration_metadata.json` and asserted by the tests.

The baseline test file is intentionally written against the OLD service. A migration candidate can be checked by preserving those externally visible assertions while changing the dependency integration.

## Verification behavior

The OLD baseline must pass before migration. A correct migration should also pass the same behavior cases after its source integration changes. A deliberately incorrect migration can be introduced by ignoring the optional `display_name` value or returning only `given_name`; the existing `user-002` or `user-001` assertion would then provide regression evidence.

No regression detector, impact analyzer, risk scorer, rollback mechanism, or UPSHIFT orchestration is included in this benchmark.

## Run the benchmark

From the repository root:

```bash
python -m pytest demo/migration_benchmark/tests/test_old_baseline.py -q
```

Run both the OLD baseline and TARGET reference tests with:

```bash
python -m pytest demo/migration_benchmark/tests -q
```

Compile the benchmark without running behavior tests:

```bash
python -m compileall -q demo/migration_benchmark
```
