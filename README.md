# UPSHIFT

UPSHIFT is an autonomous migration-risk and compatibility-proof agent for the IBM BOB 2.0 hackathon. Its purpose is to make a controlled software migration safer by identifying compatibility and regression risks, coordinating approved work through IBM Bob, independently verifying the result, and supporting safe rollback or recovery.

## Current scope

UPSHIFT is complete and verified through the final demo workflow. It runs a
controlled migration through the full safety loop: impact analysis, risk
analysis, independent behavioral verification, a decision, and, when that
decision is a rejection, a single controlled rollback with independent
re-verification of the restored baseline.

Included now:

- Read-only impact analysis (Phase 4) and explainable risk scoring (Phase 5).
- Independent verification that re-runs preserved behavior cases against a
  candidate (Phase 6) and decides on that evidence alone (Phase 7).
- A controlled recovery engine that performs at most one rollback and then
  re-verifies the restored baseline (Phase 9).
- A local read-only MCP server over STDIO exposing a single tool,
  `get_migration_context`, plus a project-scope IBM Bob configuration in
  `.bob/mcp.json`.
- A local dashboard that presents the whole loop over three routes.

Not included, by design:

- Any migration execution. UPSHIFT proves safety; it never performs a
  migration and never acts on an agent's behalf.
- Any second MCP tool, or a public MCP server.
- Public network services, cloud integrations, authentication, or multi-user
  support.
- Credential handling or unrestricted filesystem, shell, or Python execution.

## The migration safety workflow

```text
Migration request
  -> impact analysis        what would change
  -> risk analysis          how complex is it (context, not the gate)
  -> migration / Bob boundary   work an orchestrator would perform
  -> independent verification   do the preserved behaviors still hold
  -> decision                   ACCEPT or REJECT
  -> regression detection       which case failed, expected vs observed
  -> rollback                   at most one controlled attempt
  -> re-verification            does the restored baseline still pass
  -> final outcome
```

The important property: **verification is independent of the thing being
verified.** Risk never decides the outcome. Only re-running the preserved
behavior cases does.

## Why it sits alongside IBM Bob

IBM Bob is the agentic coding and orchestration layer. Bob plans and performs
migration work. UPSHIFT is the evidence and safety layer: impact, risk,
independent verification, the decision, and recovery.

They are deliberately not the same party. An agent that both applies a
migration and vouches for its safety has no independent evidence, so it cannot
be a meaningful check on itself. Bob reads UPSHIFT evidence through MCP;
UPSHIFT never acts on Bob's behalf and never approves anything automatically.

## The regression example

The demo rests on one intentional, permanent regression. A migration looks
clean and applies successfully, but verification catches a behavior change:

```text
Expected   Amazing Grace
Observed   Grace Hopper
```

That mismatch drives the decision to `REJECT`, which triggers a single
controlled rollback. The restored baseline is then re-verified and passes.
The final outcome is `MIGRATION_REJECTED_BASELINE_RESTORED` - a safe recovery,
explicitly **not** a migration success, and the original decision stays
`REJECT`.

The regression case is also runnable as a standalone demonstration via
`UPSHIFT_RUN_REGRESSION_DEMO=1`, which intentionally reports one failing test.
Do not "fix" it: a permanently failing regression is the evidence that
verification is real.

## Running the dashboard

```bash
python -m venv .venv
python -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app.api
```

Open <http://127.0.0.1:8765>. The server binds to loopback only and refuses
any other interface.

Three controlled candidates are selectable, and each is validated on the
server against a static allowlist:

| Candidate | Verification | Decision | Final outcome |
| --- | --- | --- | --- |
| `old_baseline` | PASS 5/5 | ACCEPT | `MIGRATION_ACCEPTED` |
| `correct_migration` | PASS 5/5 | ACCEPT | `MIGRATION_ACCEPTED` |
| `regression_migration` | FAIL 4/5 | REJECT | `MIGRATION_REJECTED_BASELINE_RESTORED` |

`docs/demo-guide.md` is the 2-4 minute judge-facing walkthrough.

### Real-repository mode

The controlled benchmark above is the default and is unchanged. A real local
repository can also be analyzed, by allowing a directory at start-up:

```bash
.venv\Scripts\python.exe -m app.api --allow-repository-root C:\src\billing-service
```

The flag is repeatable, and real-repository mode is closed when it is omitted.
A repository is read only when its resolved path is inside an allowed root.

UPSHIFT reads a bounded, size-capped snapshot without executing anything, so it
can report real impact and risk evidence but can never verify behavior. The
decision is therefore reported as `INCONCLUSIVE` in this mode, and that is the
honest answer rather than a gap.

The evidence is derived from the bytes on disk, and that is proved rather than
asserted: `tests/test_real_repository_evidence.py` plants a symbol at a known
line and recomputes that line from the written bytes, runs one declaration
against two different repositories, and mutates a repository to show the result
changes. If the reader were replaced with one that reads nothing, every
discovered reference and every per-symbol verification case would disappear.
See `docs/real-repository-mode.md`.

## Tests

```bash
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q app upshift_mcp demo tests
.venv\Scripts\python.exe -m pip check
```

## Architecture boundaries

IBM Bob remains the agentic coding and orchestration layer. UPSHIFT is the
migration-risk, compatibility-analysis, verification, and safety layer.

The MCP integration uses narrowly scoped read-only tools over local STDIO
transport. It must not expose a public MCP server, arbitrary shell execution,
arbitrary Python execution, credential collection, or unrestricted filesystem
access. Consequential migration changes require human approval, and a migration
is not considered successful until verification passes.

The package is named `upshift_mcp/` rather than `mcp/` because the official MCP
Python SDK is itself imported as `mcp`. While this repository root was on
`sys.path`, a local `mcp/` package shadowed the installed SDK and made
`import mcp.server` fail. See `docs/mcp-foundation.md`.

IBM Bob connects as an MCP client using the project-scope configuration in
`.bob/mcp.json`, which starts the project-local launcher `.bob/mcp_launcher.py`
over local STDIO; the launcher starts `python -m upshift_mcp` using the project
environment. Bob reads UPSHIFT evidence; UPSHIFT never acts on Bob's behalf. See
`docs/bob-mcp-integration.md`.

## Security boundaries

- The dashboard exposes exactly three routes: `GET /`, `GET /api/candidates`,
  and `POST /api/analyze`. Everything else is 404.
- The analyze request accepts exactly one field, `candidate_id`, validated
  against a static allowlist. There is no parameter for a path, a module, a
  URL, or a command.
- The MCP tool accepts a benchmark `candidate_id`, an approved repository
  *name*, and an identifier-only declaration. It accepts no path: a real
  repository must be listed by an operator in `.upshift/mcp-repository.json`,
  and with no such file the boundary is closed and the request is refused.
- There is no shell, Python-execution, subprocess, file-write, Git, credential,
  or network-out endpoint.
- Errors never echo a filesystem path, a command, or a traceback.
- The server refuses to bind a non-loopback interface.
- The MCP surface is one read-only tool; no auto-approval is configured, so
  tool use stays a human decision.

## Repository layout

```text
app/
  api/           Local dashboard: presentation model, services, HTTP layer
  core/          Impact, risk, decision, and recovery engines
  repository/    Bounded read-only repository reader and evidence types
  security/      Candidate allowlist, path boundary, capability declarations
  verification/  Independent verifier, benchmark cases, rollback provider
upshift_mcp/     Local read-only MCP integration over STDIO
demo/            Benchmark fixture and the rollback demonstration
docs/            Phase and workflow documentation, plus the demo guide
tests/           Automated checks
.bob/            IBM Bob workspace: project MCP config, launcher, and project rules
```

## Current limitations

- **No live IBM Bob runtime verification.** The MCP integration is configured
  and locally verified over STDIO - the single read-only tool is registered
  and answered from the running process. A real IBM Bob client session has not
  been observed in this environment, so no live Bob evidence is claimed, and
  the dashboard reports those three levels separately rather than collapsing
  them into a single "integrated" label.
- **Local, single-user, loopback-only.** There is no authentication and no
  multi-user support. This is an analysis view for one machine, not a hosted
  service. Exposing it would require an authentication and authorization
  layer that does not exist yet; it was not added, because weakening the bind
  restriction to make deployment easier would trade away the security boundary
  for no benefit.
- **The benchmark is a controlled fixture.** The three candidates form a
  purpose-built migration used to exercise the pipeline deterministically. The
  engines are repository-driven, but no external real-world migration has been
  demonstrated.
- **Real-repository mode cannot verify behavior.** It never executes a
  repository, so it produces static impact and risk evidence only, and its
  decision is `INCONCLUSIVE` by design. Impact evidence is also textual, so a
  reference assembled at runtime will not be found. A repository is read within
  explicit byte, file, and directory budgets, and a large one is analyzed
  partially with the shortfall reported. `docs/real-repository-mode.md` states
  these limits in full.

## Development

No environment file is required. Configuration placeholders will be introduced
only when a concrete, non-sensitive configuration need exists; secrets must
remain in ignored local environment files.
