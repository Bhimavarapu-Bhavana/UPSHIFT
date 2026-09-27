# Bob to UPSHIFT MCP integration

Phase 8.2 connects IBM Bob to the existing UPSHIFT MCP foundation. Bob can now
read UPSHIFT migration evidence over the Model Context Protocol before and
while it plans a migration.

Phase 11.2 made that connection start reliably. The original configuration only
worked if the user had activated the project virtual environment before Bob
started; it is now a self-contained project-local launcher, and it is proven to
work from an interpreter that does not have the MCP SDK installed at all.

Neither phase adds authority. UPSHIFT still executes nothing.

## Roles

**IBM Bob is the agentic coding and orchestration layer.** Bob plans the
migration, writes the code, and remains responsible for every change it makes.

**UPSHIFT is the migration-safety intelligence layer.** UPSHIFT answers four
questions and nothing else:

```text
Impact Analysis  ->  which files and call sites a migration touches
Risk Analysis     ->  how risky it is, and on what evidence
Verification      ->  do the preserved behavior cases actually pass
Decision          ->  what those three together justify
```

Bob consumes that evidence. UPSHIFT never acts on it.

## MCP connection architecture

```text
IBM Bob  (IDE or Bob Shell)
   |
   |  reads .bob/mcp.json  (project scope)
   |  starts the child process, speaks MCP over STDIO
   v
python .bob/mcp_launcher.py   .bob/mcp_launcher.py   (Phase 11.2)
   |  re-execs into the project environment
   v
python -m upshift_mcp         upshift_mcp/__main__.py
   |
   v
UPSHIFT MCP server            upshift_mcp/server.py
   |  one read-only tool
   v
get_migration_context         upshift_mcp/context.py
   |
   v
UPSHIFT evidence              app/core/impact_analyzer.py    (Phase 4)
                             app/core/risk_analyzer.py       (Phase 5)
                             app/verification/verifier.py    (Phase 6)
                             app/core/decision_engine.py     (Phase 7)
```


## Integration mechanism discovered

IBM Bob is an MCP client, and it configures MCP servers from a JSON file. From
the IBM Bob documentation (`bob.ibm.com/docs/ide/configuration/mcp/mcp-in-bob`),
there are two configuration levels:

- **Global**: `~/.bob/settings/mcp.json`, applies across all workspaces.
- **Project**: `.bob/mcp.json` in the project root, shareable with the team
  through version control.

Project-level configuration takes precedence over global configuration when
server names conflict. UPSHIFT uses the **project** level, so the configuration
travels with the repository.

This repository already reserved `.bob/` as the "Reserved IBM Bob workspace", so
this is the location the project intended.

## Exact MCP configuration

The integration is two files, `.bob/mcp.json` and the launcher it names:

```json
{
  "mcpServers": {
    "upshift-migration-evidence": {
      "command": "python",
      "args": ["${workspaceFolder}/.bob/mcp_launcher.py"],
      "cwd": "${workspaceFolder}",
      "disabled": false
    }
  }
}
```

Every field is a documented IBM Bob STDIO parameter:

| Field | Value | Meaning |
| --- | --- | --- |
| `command` | `python` | the executable Bob launches |
| `args` | `["${workspaceFolder}/.bob/mcp_launcher.py"]` | the project-local launcher |
| `cwd` | `${workspaceFolder}` | project root, so the launcher resolves its own project |
| `disabled` | `false` | the server is enabled |

Phase 11.2 changed `args` from `["-m", "upshift_mcp"]` to the launcher. The
reason is recorded in the next section; the summary is that a bare `python` on
`PATH` is not the interpreter that has the project dependencies.

`alwaysAllow` is deliberately **absent**. In IBM Bob, auto-approval is per-tool
and off by default, so leaving it out keeps every tool call an explicit human
approval step. `env` is deliberately **absent** because UPSHIFT needs no
credentials.

There is no `url` and no `type`, so the configuration cannot describe a
network transport. STDIO is selected by the absence of a network transport, and
confirmed by `upshift_mcp.server.TRANSPORT == "stdio"`.

## Why the launcher exists

The Phase 8.2 configuration launched `python -m upshift_mcp` and required a user
to activate the project virtual environment first. That is a real defect, not a
stylistic choice:

- The project dependencies, including the official `mcp==2.2.0` SDK, are
  installed in `.venv/`, not in a system interpreter.
- `python` on `PATH` resolves to whichever interpreter comes first. On the
  machine used to build this, that was `C:\Program Files\Python314\python.exe`,
  which has no `mcp` package at all.
- Bob is not guaranteed to be launched from a shell where the user activated
  `.venv`, and Bob's documentation does not ask the user to activate a virtual
  environment before it starts an MCP server.
- The failure mode was silent in intent and total in effect: the server could
  not import, so the tool never appeared in Bob at all. A user would have had to
  diagnose a missing package inside a child process Bob owns.

The measured consequence, before the launcher existed: the focused Bob tests
passed 20 of 20 with `.venv\Scripts` on `PATH` and failed all 20 without it.

`.bob/mcp_launcher.py` removes the dependency on how Bob was started. It:

- derives the project root from its own file location, so it works regardless
  of the working directory Bob used;
- prefers a project-relative interpreter (`.venv/Scripts/python.exe`,
  `.venv/bin/python`), cross-platform;
- serves the MCP server in place when the interpreter that invoked it already
  has the SDK, so the common case spawns no extra process;
- otherwise starts exactly one child, `python -m upshift_mcp`, with that
  project interpreter, the project root as its working directory, inherited
  STDIO, and `shell=False`;
- ignores any extra arguments, so a host that appends something cannot turn
  the launcher into an argument injection point;
- prints a traceback to stderr, which is the diagnostic channel, and never
  writes to stdout, which belongs to the MCP protocol.

Measured cost of the extra process, over ten sessions each: through the
launcher 1.17-1.27s (median 1.20s), against a single process 1.12-1.30s
(median 1.16s). The launcher is not on the critical path.

## Exact MCP entry point

```text
command : python
args    : ${workspaceFolder}/.bob/mcp_launcher.py
cwd     : ${workspaceFolder}
then    : python -m upshift_mcp   (in .venv, project root)
module  : upshift_mcp/__main__.py
```

`python -m upshift_mcp` runs `upshift_mcp.server.main()`, which calls
`server.run(transport="stdio")`. Importing the package does not start a server,
so the process only serves the MCP connection Bob opened.

The interpreter that actually serves MCP is the project `.venv`, resolved by the
launcher. `mcp==2.2.0` is present in `.venv/Scripts/../Lib/site-packages/mcp`.
A user no longer has to activate the environment for Bob to work.

## STDIO transport

Bob starts the server as a child process and exchanges MCP messages over that
process's standard input and output. There is no port, no listener, and no
network traffic. Bob IDE and Bob Shell each start their own child process.

Not implemented and not used by this integration: SSE, Streamable HTTP, any
other HTTP transport, a public MCP server, a remote MCP endpoint, and cloud
deployment.

## Tool available to Bob

One tool, unchanged from Phase 8.1:

| Tool | Purpose |
| --- | --- |
| `get_migration_context` | read-only migration evidence for one candidate |

The server advertises it with `read_only_hint: true`,
`destructive_hint: false`, and `open_world_hint: false`.

Its parameters are `candidate_id`, restricted to the allowlist
`old_baseline`, `correct_migration`, and `regression_migration`, and — added in
Phase 11.1 — an optional `repository` name plus a `migration` declaration for
real-repository evidence. Passing neither `repository` nor `migration` reads the
controlled benchmark exactly as before. `repository` is the *name* of a
directory an operator approved in `.upshift/mcp-repository.json`; it is never a
path, and no parameter accepts one. See `docs/real-repository-mode.md`.

No second tool was added in this phase. `get_migration_context` already carries
impact, risk, verification, and decision evidence, which is everything Bob needs
to plan safely. A second tool would widen the surface without adding a required
capability.

### What Bob can read

- `migration_name`, `old_api`, `target_api`
- `impact.impacted_files` and the file count
- `impact.direct_references` and their count
- `impact.target_references` and their count
- `risk.risk_level`, `risk.risk_score`, `risk.risk_factors`, `risk.explanations`
- `verification.verification_status`
- `verification` counts: total, passed, failed, inconclusive, skipped
- `verification.results`, including each case's expected and observed value
- `decision.decision_state` and whether it is accepted
- `decision.explanations` and `decision.evidence`

### What Bob cannot execute through UPSHIFT

UPSHIFT exposes no way to act. Bob cannot, through this server:

- execute a migration
- modify any repository file
- run a shell command
- execute Python dynamically
- execute an arbitrary subprocess
- pass an arbitrary filesystem path
- access credentials, secrets, or `.env`
- reach any file outside the controlled UPSHIFT data and evidence boundary
- make a network request
- modify Git state
- trigger rollback, recovery, or replanning
- obtain automatic approval of a consequential change

This is enforced, not merely documented. The import allowlist means the package
cannot even import `subprocess`, `os`, `socket`, or any HTTP client. The only
input is an allowlisted identifier; path-like and command-like values are
refused with a deliberate error naming the allowlist, and execution-shaped
arguments are ignored rather than honored.

## Human control

Nothing in this phase approves a migration automatically.

- Every Bob tool call remains subject to Bob's normal approval flow, because
  `alwaysAllow` is not set.
- A `REJECT` or `INCONCLUSIVE` decision is evidence to surface, never a result
  to retry until it turns into `ACCEPT`.
- Only a `PASS` verification-backed `ACCEPT` satisfies the safety gate.
- UPSHIFT never marks a migration successful on its own.

`.bob/rules/upshift-migration-safety.md` is a Bob project rule that tells Bob to
consult UPSHIFT before planning and again before claiming success, and restates
these boundaries to the model. Project rules are the supported Bob mechanism for
this; Bob has no agent harness hooks.

## How the integration was verified

`tests/test_bob_mcp_integration.py` and `tests/test_bob_launcher.py` read the real
`.bob/mcp.json` and use its `command`, `args`, and `cwd` to start the real server
as a child process. They then perform a real MCP handshake with the official
client SDK, the same protocol Bob speaks. The server is not mocked, the launcher
is not stubbed, and no Bob response is fabricated. Where a test needs an
interpreter that genuinely lacks the SDK, it uses the real system interpreter
rather than pretending to.

The suite covers: the configuration pointing at the launcher; the launcher
resolving the project environment; STDIO being the only transport; a real
handshake and a single-tool surface; the read-only annotation; the benchmark
evidence being identical whether the server is started through the launcher or
directly; unknown tools being refused; command-like and path-like arguments
being refused; extra and execution-shaped arguments being ignored and inert;
stdout carrying protocol bytes only; the repository being byte-identical before
and after; no network configuration; a child process that binds no listening
socket; the live-Bob status staying unverified; no Bob client being simulated
anywhere; the benchmark rollback still restoring the baseline; and the Phase 8.1
and Phase 1-7 suites still passing.

The portability claim is tested the hard way, not asserted. The system
interpreter used to build this, `C:\Program Files\Python314\python.exe`, has no
`mcp` package. The test suite launches the configured entry point with that
interpreter and asserts the handshake still completes, which is the only
arrangement that could prove the launcher solves the problem it was added for.

## Verification status

Be precise about what has and has not been exercised.

### VERIFIED LOCALLY

- The `.bob/mcp.json` project-scope configuration is valid against the IBM Bob
  STDIO schema and registers the correct entry point.
- An interpreter with no `mcp` package installed starts the real UPSHIFT MCP
  server successfully, through the launcher, with no environment activation.
- A real MCP STDIO handshake completes, and `get_migration_context` is
  discovered and called over that connection.
- Exactly one tool is exposed, and its input parameters are unchanged:
  `candidate_id`, `migration`, `repository`.
- The tool is advertised as read-only and returns real impact, risk,
  verification, and decision evidence.
- The benchmark evidence is byte-identical through the launcher and through a
  direct start: 18 impacted files, 112 direct references, 116 target references,
  `HIGH` risk at score 266.
- Rejected inputs are refused deliberately; execution-shaped arguments are
  inert; the repository is unchanged; no socket is bound.
- The Phase 8.1 and Phase 1-7 suites still pass.

### REQUIRES LIVE IBM BOB UI/RUNTIME VERIFICATION

No IBM Bob runtime is installed in this environment. This was searched for
rather than assumed. There is no `bob` executable on `PATH`, no Bob entry in the
installed-application registry, no running Bob process, no `~/.bob`
configuration directory, no Bob extension in the installed VS Code-family
editor extensions, no Bob package in the global npm install, and no Bob shortcut
or WindowsApps entry. A filesystem scan surfaced only `obs-studio`, which is OBS
Studio and unrelated.

The integration was therefore exercised with a standards-compliant MCP client
speaking the same protocol over the same STDIO child process, which is the
server-side half of the contract.

The following still require a live Bob IDE or Bob Shell:

- Bob loading `.bob/mcp.json` and listing `upshift-migration-evidence` as
  enabled in the MCP settings panel.
- Bob resolving `${workspaceFolder}` to this project root.
- Bob's model choosing to call `get_migration_context` from the project rule,
  and Bob rendering the returned evidence.
- Bob's approval prompt appearing for each tool call.

The dashboard reports this distinction rather than collapsing it. Its
integration status has three levels: `configured` (`configured`, by local file
read), `local_mcp` (`verified`, by the local surface and the test suite), and
`live_bob` (`not_verified`, with no `verified_by` at all).

## Manual Bob configuration step

The configuration file and launcher are already committed, so a user only needs
to run Bob against this repository:

1. Open this repository in IBM Bob IDE, or run `bob` from this directory. There
   is no need to activate the virtual environment first; that requirement was
   removed in Phase 11.2.
2. Open the MCP settings panel (gear icon, then **MCP**). Bob creates
   `.bob/mcp.json` on demand; here it already exists.
3. Confirm `upshift-migration-evidence` is listed and enabled. If it is not
   listed, click the reload icon next to the server list.
4. Expand the server and confirm `get_migration_context` is enabled.
5. Ask Bob, for example: "Before planning this migration, use the
   `upshift-migration-evidence` server to call `get_migration_context` for
   `correct_migration` and summarize the impact, risk, and verification
   evidence."
6. Confirm Bob prompted for approval before running the tool.

If the server does not appear, check that `python` runs at all, and that
`.bob/mcp_launcher.py` is present next to `.bob/mcp.json`.

## Limitations caused by Bob availability

- Bob's own validation queries could not be run, so Bob-side tool selection
  behavior is unverified.
- Auto-approval, approval prompting, and per-tool enable/disable are Bob
  features that only a live Bob session can exercise.
- Bob's project-rule loading behavior is unverified.
- Nothing in this phase depends on a Bob API, endpoint, or credential, so the
  absence of Bob credentials is not a blocker for the server side.

## Files

- `.bob/mcp.json`: IBM Bob project-scope MCP configuration for UPSHIFT.
- `.bob/mcp_launcher.py`: project-environment launcher named by that
  configuration, so Bob starts MCP with the interpreter that has the SDK
  regardless of how Bob itself was started.
- `.bob/rules/upshift-migration-safety.md`: Bob project rule describing when to
  consult UPSHIFT and the boundaries Bob must respect.
- `tests/test_bob_mcp_integration.py`: integration tests driving the real
  server through the real configuration.
- `tests/test_bob_launcher.py`: Phase 11.2 tests for the launcher, the
  interpreter that has no SDK, the one-tool surface, and the unverified live
  Bob status.
- `docs/bob-mcp-integration.md`: this document.
