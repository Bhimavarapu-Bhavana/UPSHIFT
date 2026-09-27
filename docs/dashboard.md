# UPSHIFT Dashboard (Phase 10.1)

## Purpose

The dashboard is a **presentation and control surface only**. It shows the
result of the existing UPSHIFT pipeline and lets a user run that pipeline for
one controlled migration candidate.

UPSHIFT's promise is that it maps migration compatibility risk, independently
verifies behaviour, detects regressions, and supports safe rollback. This
dashboard makes that proof visible. It computes nothing of its own.

```text
User
  -> UPSHIFT Dashboard
  -> migration request (one controlled candidate)
  -> existing UPSHIFT engines
  -> structured result
  -> dashboard display
```

## What it does not do

The dashboard does not reimplement, re-score, or second-guess any analysis. It
calls the existing engines and copies their output:

| Stage | Existing module | Owner |
| --- | --- | --- |
| Impact | `app.core.impact_analyzer` | Phase 4 |
| Risk | `app.core.risk_analyzer` | Phase 5 |
| Verification | `app.verification.verifier` | Phase 6 |
| Decision | `app.core.decision_engine` | Phase 7 |
| Rollback provider | `app.verification.benchmark_rollback` | Phase 9 |
| Recovery | `app.core.recovery_engine` | Phase 9 |

`app/api/dashboard_service.py` is the only new orchestration. It calls each
engine once and passes the untouched reports to the presentation model.

## How to start it

```powershell
.venv\Scripts\python.exe -m app.api
```

Then open `http://127.0.0.1:8765`.

The server binds to loopback only. It is a local analysis view, not a public
service, and the entry point refuses a non-loopback bind.

Options: `--host`, `--port`, and the repeatable `--allow-repository-root PATH`.

## Controlled candidates

The user may only select these statically allowlisted benchmark candidates:

```text
old_baseline
correct_migration
regression_migration
```

Any other value is refused before an engine runs, including path-like and
command-like input.

## Analysis modes

The request panel has two modes. The dashboard reports which are available based
on what the server reports, and does not decide availability itself.

**Demo mode** is the controlled benchmark above. It is always available and is
unchanged by real-repository mode.

**Real repository mode** reads an operator-allowed local repository. It is
**closed by default** and becomes available only when the server was started with
`--allow-repository-root`. When it is closed, the option is shown as unavailable
together with the reason, rather than being hidden.

In real-repository mode the panel asks for a repository folder, a migration name,
a current entry point, and a target entry point. It never asks for a command, a
module to import, or a branch to check out.

A result from this mode carries an extra **Repository Read Evidence** section
naming the repository, the files inspected, the files skipped and why, the bytes
read, the restoration mechanism, and the read restrictions that were applied. It
never contains an absolute filesystem path.

Because UPSHIFT does not execute a repository, this mode reports verification
and decision as `INCONCLUSIVE`. See `docs/real-repository-mode.md`.

## Displayed sections

1. **Migration** - migration name and candidate id.
2. **Impact (Phase 4)** - impacted file count, direct references, target
   references, and renamed symbols.
3. **Risk (Phase 5)** - risk level, risk score, and the scored risk factors.
4. **Verification (Phase 6)** - status, passed, failed, and inconclusive case
   counts, plus per-case evidence for anything that did not pass.
5. **Decision (Phase 7)** - `ACCEPT`, `REJECT`, or `INCONCLUSIVE`.
6. **Recovery (Phase 9)** - the state path visited, whether rollback was
   attempted and succeeded, the final verification status, the final candidate,
   whether re-planning is required, and a clear final outcome.
7. **Repository Read Evidence** - real-repository mode only, as described above.

### Reading a recovery outcome

For the regression candidate the dashboard shows the real sequence:

```text
verification FAIL
  -> decision REJECT
  -> ROLLBACK_REQUIRED
  -> ROLLING_BACK
  -> ROLLBACK_VERIFICATION
  -> restored baseline verification PASS
  -> COMPLETED
```

`COMPLETED` after a rollback means **the failed migration was safely recovered
and the verified baseline was restored**. It does **not** mean the migration
succeeded. The dashboard states the original decision separately, and the
final outcome for that candidate is reported as
`MIGRATION_REJECTED_BASELINE_RESTORED`, never as an acceptance.

## HTTP surface

The complete route table is three routes:

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Serve the dashboard document. |
| `/api/candidates` | GET | List controlled candidates, the service boundary, and both analysis modes. |
| `/api/analyze` | POST | Run the pipeline for one input. |

Any other path returns `404`; any other method on these paths returns `405`.

`POST /api/analyze` accepts exactly one of two shapes, never a blend of them:

```json
{"candidate_id": "correct_migration"}
```

```json
{"repository_path": "/abs/path", "migration": {"name": "...", "old_api": "...", "target_api": "..."}}
```

## Safety restrictions

The dashboard deliberately exposes **no** capability to:

- run shell, Python, or any subprocess;
- run repository code, import a repository module, or evaluate repository content;
- read or write a filesystem path outside the operator-allowed repository
  boundary, or select a repository the operator did not allow;
- run Git commands, resets, or force operations;
- supply a command, script, module path, or callable;
- collect credentials, secrets, or environment values;
- invoke MCP tools or IBM Bob;
- bypass approval, or accept a migration automatically.

In demo mode the only caller-supplied value anywhere is one `candidate_id`,
validated against a static allowlist. The controlled rollback provider is
constructed internally and always closed, so a request cannot supply its own
provider or leave a temporary working copy behind.

In real-repository mode a caller may additionally supply a `repository_path` and a
`migration` declaration. The path is accepted only when it resolves inside a
boundary root the operator allowed at start-up, and the boundary is held on the
application rather than in the request, so a request can never widen it. The
declaration is reduced to dotted identifiers and can name no file, module, or
command. The repository is opened read-only, credential and key files are never
read, links are never followed, and the read is capped by explicit byte, file,
and directory budgets.

Request bodies are size-capped at 4,096 bytes and must be a JSON object carrying
exactly one of the two accepted shapes. Responses never echo a filesystem path, a
command, or a traceback, and a real-repository result never contains an absolute
path at all.

## Focused tests

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard.py tests/test_dashboard_ui.py tests/test_repository_input.py -q
```

They cover application start-up, candidate validation (allowed, unknown,
path-like, command-like, and non-string), the correct migration, the full
regression and recovery sequence through the real HTTP endpoint, the unchanged
Phase 4-9 evidence, the absence of an arbitrary-execution surface, and the real
repository boundary, reader policies, and honest `INCONCLUSIVE` reporting.

## Dependencies

The dashboard uses `starlette` and `uvicorn`, which are already installed as
dependencies of the pinned `mcp` package. Phase 10.1 adds no new dependency and
installs nothing. `requirements.txt` is deliberately left untouched so the
Phase 8 pinning guard stays valid; declaring `starlette` and `uvicorn`
explicitly is deferred to a later phase.
