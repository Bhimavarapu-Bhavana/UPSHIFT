# MCP foundation

Phase 8.1 establishes the local MCP foundation for UPSHIFT. This phase adds a
STDIO-only MCP server that exposes one controlled, read-only tool so that an
external agent can read UPSHIFT migration evidence. It deliberately stops short
of executing migrations and of integrating IBM Bob.

## Why the namespace was renamed

The Phase 1 scaffold reserved a repository-level package named `mcp/`. The
official MCP Python SDK is also imported as `mcp`.

That collision was not cosmetic. Because this repository is tested from its own
root, the root directory is placed on `sys.path`, so `import mcp` resolved to
the local placeholder instead of the installed SDK:

```text
import mcp            -> UPSHIFT/mcp/__init__.py   (placeholder)
import mcp.server     -> ModuleNotFoundError
```

The placeholder package had no `server` submodule, so any future
`from mcp.server... import ...` inside this repository would have failed. The
local package was therefore renamed:

```text
mcp/  ->  upshift_mcp/
```

The rename removes the collision without changing the scaffold's meaning, the
package's responsibilities, or the STDIO-only integration boundary.
`upshift_mcp/__init__.py` remains import-cheap: importing it does not import the
SDK and does not start a server.

Only references that depended on the package name were updated:

- `tests/test_scaffold.py`
- the repository layout section of `README.md`
- this document

## Official MCP SDK version

The foundation pins the official SDK:

```text
mcp==2.2.0
```

The verified import for this version is:

```python
from mcp.server.mcpserver import MCPServer
```

`FastMCP` is not used, because it is not available in the verified installed SDK
version. All three of the following must resolve to the installed
site-packages SDK, never to the local UPSHIFT package:

```python
import mcp
import mcp.server
from mcp.server.mcpserver import MCPServer
```

## MCP server architecture

```text
IBM Bob  (future, not implemented)
   |
   v
UPSHIFT MCP server            upshift_mcp/server.py
   |                          transport: STDIO only
   |  read-only tool call
   v
get_migration_context         upshift_mcp/server.py -> upshift_mcp/context.py
   |
   v
UPSHIFT evidence layers       app/core/impact_analyzer.py   (Phase 4)
                              app/core/risk_analyzer.py      (Phase 5)
                              app/verification/verifier.py   (Phase 6)
                              app/core/decision_engine.py    (Phase 7)
```

- `upshift_mcp/context.py` builds the structured evidence mapping. It performs
  no new analysis and invents no migration results: every value is copied from
  an existing UPSHIFT report object. It reads no file directly, and delegates
  all file access to the controlled evidence layers.
- `upshift_mcp/server.py` wires that mapping into the official SDK and defines
  the transport. Importing it does not start a server.
- Both modules expose a single narrow input, `candidate_id`, which must appear
  in the static benchmark allowlist (`old_baseline`, `correct_migration`,
  `regression_migration`).

## STDIO transport

`TRANSPORT = "stdio"` is the only supported transport, and
`run_mcp_server()` calls `server.run(transport=TRANSPORT)`.

The following are intentionally not implemented in this phase:

- SSE
- Streamable HTTP
- any other HTTP transport
- a public MCP server
- remote MCP
- network listeners or cloud deployment

## `get_migration_context`

`get_migration_context` returns structured, read-only migration context for one
allowlisted candidate. It is registered with the SDK as a read-only tool and
annotated accordingly:

```python
ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
```

The response projects the existing Phase 4-7 concepts:

| Field | Source |
| --- | --- |
| `migration_name` | `ImpactReport` / benchmark metadata |
| `impact.impacted_files` | `ImpactReport.impacted_files` |
| `impact.direct_references` | `ImpactReport.direct_references` |
| `impact.target_references` | `ImpactReport.target_references` |
| `risk.risk_level` | `RiskReport.risk_level` |
| `risk.risk_score` | `RiskReport.risk_score` |
| `verification.verification_status` | `VerificationReport.status` |
| `verification` case counts | `VerificationReport` counts |
| `candidate_id` | `VerificationReport.candidate_id` |
| `decision.decision_state` | `DecisionReport.decision` |
| `decision.explanations` / `decision.evidence` | `DecisionReport` |

No migration result is invented. The tool reports what the existing analyzers,
verifier, and decision engine concluded for the requested candidate.

A rejected `candidate_id` is refused deliberately. The registered tool
translates the allowlist failure into an MCP `ToolError`, so the client receives
a controlled error result rather than an unhandled server crash, and no evidence
is gathered on the way out.

## Read-only security boundary

The tool is read-only by construction:

- its only input is an allowlisted `candidate_id`; there is no parameter for a
  filesystem path, a module path, or a command;
- it has no write path: the package imports no writer and calls no mutating
  primitive, and the focused tests snapshot SHA-256 digests of every repository
  file before and after a tool call to prove nothing changes;
- it opens no network connection: the tests import the server in a clean
  subprocess where socket creation and `MCPServer.run` both raise, and the
  package's imports are checked against a minimal allowlist that contains no
  network module.

### Prohibited capabilities

The following are prohibited and are not implemented:

- executing migrations
- modifying repository files
- running shell commands
- executing Python dynamically
- executing arbitrary subprocesses
- accepting arbitrary filesystem paths
- accessing credentials
- reading secrets
- accessing `.env`
- accessing files outside the controlled UPSHIFT data and evidence boundary
- making network requests
- modifying Git state
- invoking IBM Bob automatically
- performing rollback
- performing recovery
- performing replanning

These are declared in `upshift_mcp.context.PROHIBITED_CAPABILITIES` so the
boundary is data, not only prose.

## Relationship to future IBM Bob integration

The intended end state is:

```text
IBM Bob
   v
future MCP integration
   v
UPSHIFT MCP server
   v
migration evidence / context
```

IBM Bob remains the agentic coding and orchestration layer. UPSHIFT remains the
migration-risk, compatibility-analysis, verification, and safety layer. The MCP
server is the narrow, read-only seam between them: Bob would read evidence and
risk from UPSHIFT rather than UPSHIFT executing work on Bob's behalf.

## Why Bob integration is not implemented yet

This phase is a foundation, and the integration is deliberately deferred:

- no Bob credentials or credential handling are introduced;
- no Bob API calls, Bob-specific execution, or automatic Bob invocation exist;
- no migration execution, rollback, recovery, or replanning exists;
- no dashboard, cloud deployment, or public server exists.

Adding those would cross the read-only boundary that this phase is built to
establish. The evidence seam must be provably inert before it is connected to
anything that can act.

## Files

- `upshift_mcp/__init__.py`: package docstring explaining the rename. Remains
  import-cheap and starts no server.
- `upshift_mcp/context.py`: read-only evidence assembly and the declared
  security boundary.
- `upshift_mcp/server.py`: the STDIO-only `MCPServer` and the
  `get_migration_context` tool.
- `upshift_mcp/__main__.py`: console entry point, `python -m upshift_mcp`.
- `tests/test_mcp_foundation.py`: focused tests for SDK import safety, the
  unshadowed `mcp` SDK, the STDIO transport, the tool, and the read-only
  boundary.
- `tests/test_scaffold.py`: scaffold smoke test, updated to the renamed package.
- `requirements.txt`: pins `mcp==2.2.0`.
