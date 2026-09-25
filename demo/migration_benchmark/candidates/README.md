# Migration Candidates

This directory contains two deterministic candidate implementations for the profile-label migration benchmark.

## Why two candidates exist

The candidates make the migration tradeoff inspectable without adding UPSHIFT intelligence:

- `correct_migration/` demonstrates a successful API migration that preserves behavior.
- `regression_migration/` demonstrates a superficially plausible migration that violates one compatibility invariant.

Both candidates use the existing local TARGET API in `demo/migration_benchmark/target/` as their dependency. Neither candidate changes the OLD baseline or acts as a migration engine.

## Correct migration

`correct_migration/profile_service.py`:

- Replaces `LegacyDirectory.lookup()` with `ProfileDirectory.get_profile()`.
- Replaces `first_name` and `last_name` with `given_name` and `family_name`.
- Preserves the optional `display_name` behavior inherited from the OLD `preferred_name` field.
- Preserves missing-profile and empty-name behavior.

Its tests cover the required outputs:

| User | Required label |
| --- | --- |
| `user-001` | `Ada Lovelace` |
| `user-002` | `Amazing Grace` |
| `user-003` | `Unknown` |
| `empty-user` | `Unnamed user` |
| `missing-user` | `Unknown user` |

Run the correct candidate tests with:

```bash
python -m pytest demo/migration_benchmark/candidates/correct_migration/tests -q
```

Expected result: all tests pass.

## Regression migration

`regression_migration/profile_service.py` calls the TARGET `get_profile()` method and uses the renamed name fields, but intentionally ignores `profile.display_name` and always builds the label from `given_name` and `family_name`.

This violates this exact compatibility invariant:

> When the optional TARGET `display_name` is present, it has the same behavior as the OLD `preferred_name` and must take precedence over the assembled name parts.

For `user-002`, the expected label is `Amazing Grace`, while this candidate returns `Grace Hopper`.

The regression test is intentionally skipped during normal test discovery unless `UPSHIFT_RUN_REGRESSION_DEMO=1` is set. This keeps the repository suite green while still allowing the failure to be demonstrated explicitly.

Run the regression demonstration in PowerShell with:

```powershell
$env:UPSHIFT_RUN_REGRESSION_DEMO = "1"
python -m pytest demo/migration_benchmark/candidates/regression_migration/tests/test_regression_demo.py -q
Remove-Item Env:UPSHIFT_RUN_REGRESSION_DEMO
```

Expected result: one test fails, the assertion reports expected `Amazing Grace` versus actual `Grace Hopper`, and pytest exits with a non-zero status. This failure is intentional regression evidence, not a defect in the normal suite.

No regression detector, impact analysis, risk scoring, rollback, MCP server, IBM Bob connection, or dashboard is included in this candidate fixture.
