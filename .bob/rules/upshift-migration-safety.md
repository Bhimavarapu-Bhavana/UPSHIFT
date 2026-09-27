# UPSHIFT migration safety

This project uses the `upshift-migration-evidence` MCP server. It is a local,
read-only evidence source. It never changes files and never runs code.

## When to consult UPSHIFT

Call the `get_migration_context` tool on that server **before** you plan or
start any API migration, and again **after** you produce a candidate, before
you claim the migration works.

Ask for the candidate you are working on:

- `old_baseline` - the pre-migration state
- `correct_migration` - a completed, verified candidate
- `regression_migration` - a deliberately broken candidate

## How to use the result

- `impact` tells you which files, direct references, and target references the
  migration touches. Use it to build your change plan and to avoid missing a
  call site.
- `risk` tells you the risk level, score, and the evidence behind them. High
  risk means widen your review; it does not mean the migration is wrong.
- `verification` tells you whether the preserved behavior cases actually pass.
- `decision` is UPSHIFT's verdict. Read `explanations` and `evidence` for why.

## Boundaries you must respect

- UPSHIFT is an intelligence layer, not an executor. You remain responsible
  for every code change, and UPSHIFT will never make a change for you.
- You may not mark a migration successful on your own. Only a verification
  status of `PASS` with a decision of `ACCEPT` satisfies the safety gate.
- A `REJECT` or `INCONCLUSIVE` decision must be surfaced to the user, not
  retried until it turns into `ACCEPT`.
- Never treat UPSHIFT output as approval to skip verification.
- Do not use UPSHIFT to approve consequential changes on the user's behalf.
