# UPSHIFT hackathon demo guide

A 2-4 minute walkthrough of the full safety loop. Every number, label, and
outcome below is produced by the existing UPSHIFT engines. Nothing in the
dashboard is scripted, mocked, or pre-canned.

## The one-line pitch

> IBM Bob can perform a migration. UPSHIFT independently proves whether the
> migration is actually safe.

Bob is the agent that *does* the work. UPSHIFT is the party that *checks* the
work, and that is allowed to say no.

## Start the dashboard

```bash
.venv\Scripts\python.exe -m app.api
```

Then open <http://127.0.0.1:8765>.

The server binds to loopback only. It refuses any other interface, so the demo
runs on the presenting machine and is not reachable from the network.

The page loads with `old_baseline` selected. Click **Analyze Migration**.

---

## 1. Problem

An agentic coding tool can apply a large mechanical migration across a
repository in seconds. It reports that it finished. It does not prove that the
result still behaves the way the code behaved before.

That gap is the problem. A migration can compile, apply cleanly, and still
silently change what the software returns to a user.

## 2. Migration request

Select `regression_migration` and click **Analyze Migration**.

The dashboard shows the controlled candidate, the migration under evaluation,
and the OLD to TARGET API pair. The candidate is a fixed allowlist entry
validated on the server. There is no field for a path, a command, or a script.

## 3. UPSHIFT analysis

Two analyses run first, and both are read-only text analysis over the
repository:

- **Impact** - how many files and references are in scope.
- **Risk** - an explainable compatibility score. For this migration it is
  `HIGH` with a score of `266`.

Say the important part out loud: **risk is context, not the gate.** A high risk
score means the migration is complex. It does not decide anything on its own.

## 4. Bob execution boundary

Point at the fourth step of the safety chain.

UPSHIFT never applies a migration. The candidate stands in for work an
orchestrator such as IBM Bob would perform. The boundary is explicit in the
interface: Bob does the coding and orchestration, UPSHIFT provides the
evidence and the veto.

The **IBM Bob & MCP Status** panel reports exactly how far that integration is
verified, and keeps three levels apart:

- **MCP server configured** - `.bob/mcp.json` declares the read-only UPSHIFT
  evidence server. Verified by local file inspection.
- **Local MCP integration verified** - the one documented read-only tool
  (`get_migration_context`) is registered and answered from this running
  process over STDIO. Verified by in-process introspection.
- **Live IBM Bob runtime** - **not verified.** No live IBM Bob client session
  has been observed in this environment, so no live Bob evidence is claimed.

## 5. Independent verification

UPSHIFT re-runs the five preserved behavior cases against the candidate,
independently of whatever produced it.

For the correct migration this is `PASS`, 5 of 5. For the regression it is
`FAIL`, 4 of 5, and the evidence panel shows the expected value beside the
observed value for every case.

## 6. Failure detection

This is the moment the demo is built around. The proof block at the top of the
safety chain shows the real mismatch returned by the verifier:

```
Expected   Amazing Grace
Observed   Grace Hopper
```

This regression is intentional and permanent. It is what proves the verifier
is actually looking at behavior rather than re-reporting what the migration
claimed to do.

## 7. Automatic controlled rollback

The failed case drives the decision to `REJECT`, which triggers exactly one
controlled rollback through the temporary-copy provider. No Git state is
touched, and the dashboard exposes no way to ask for more than one attempt.

The recovery timeline shows the states the engine actually passed through:

`ROLLBACK_REQUIRED -> ROLLING_BACK -> ROLLBACK_VERIFICATION -> COMPLETED`

## 8. Re-verification

Recovery ending in `COMPLETED` does **not** mean the migration succeeded. The
restored baseline is put back through the same independent cases, and the result
is `PASS` for baseline verification.

This is the point most tools get wrong. The rollback is not the proof; the
re-verification of what was restored is the proof.

## 9. Final safety decision

The final outcome is `MIGRATION_REJECTED_BASELINE_RESTORED`, and the dashboard
states in plain words that this is a safe recovery, not a migration success.

The original decision remains `REJECT`. The final candidate is
`restored_baseline`. The migration was **not** accepted.

The safe outcome is that the bad migration did not ship and the known-good
baseline is back in place, proven by evidence.

---

## Running the other two scenarios

| Candidate | Verification | Decision | Recovery | Final outcome |
| --- | --- | --- | --- | --- |
| `old_baseline` | PASS 5/5 | ACCEPT | `ACCEPTED -> COMPLETED` | `MIGRATION_ACCEPTED` |
| `correct_migration` | PASS 5/5 | ACCEPT | `ACCEPTED -> COMPLETED` | `MIGRATION_ACCEPTED` |
| `regression_migration` | FAIL 4/5 | REJECT | `ROLLBACK_REQUIRED -> ROLLING_BACK -> ROLLBACK_VERIFICATION -> COMPLETED` | `MIGRATION_REJECTED_BASELINE_RESTORED` |

Run `correct_migration` after the regression so the judges see that the same
harness accepts a genuinely correct migration. The difference between the two
runs is only the candidate - the pipeline, the gate, and the recovery path are
identical.

## The regression demonstration is opt-in

The incorrect candidate is also available as a standalone demonstration:

```bash
$env:UPSHIFT_RUN_REGRESSION_DEMO="1"
.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:\UPSHIFT_RUN_REGRESSION_DEMO
```

This intentionally produces **one failing test** showing the same expected and
observed values. The default suite skips it. Do not "fix" it - a permanently
failing regression case is the evidence that verification is real.

## Security boundaries, stated plainly

- Three routes only: `GET /`, `GET /api/candidates`, `POST /api/analyze`.
- The analyze request accepts exactly one field, `candidate_id`, checked
  against a static allowlist.
- No shell, Python execution, subprocess, file write, Git command, credential,
  or network-out endpoint exists.
- The MCP surface is a single read-only tool over local STDIO. Bob can read
  evidence; UPSHIFT never acts on Bob's behalf, and no approval is automated.
- The server refuses to bind anything but loopback.

## Current limitations

- **No live IBM Bob runtime verification.** The MCP integration is configured
  and locally verified over STDIO. A real Bob session was not observed, so the
  dashboard says so rather than implying one.
- **Local, single-user, loopback-only.** There is no authentication and no
  multi-user support, by design. The dashboard is an analysis view for the
  presenting machine, not a hosted service.
- **The benchmark is a controlled fixture.** The three candidates are a
  purpose-built profile-label directory migration used to exercise the
  pipeline deterministically. The engines are repository-driven and would run
  against a real migration, but that has not been demonstrated here.
