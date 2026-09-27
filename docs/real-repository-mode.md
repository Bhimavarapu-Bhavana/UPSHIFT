# UPSHIFT Real-Repository Mode

Real-repository mode is the second input to UPSHIFT. The controlled benchmark
stays exactly as it was; this mode points the same engines at a real local
repository that you explicitly allow.

## What it is, and what it is not

UPSHIFT reads a bounded snapshot of an operator-allowed local repository and
feeds it to the existing Phase 4-9 engines. It adds no analysis of its own.

| | Demo mode | Real-repository mode |
| --- | --- | --- |
| Input | Controlled benchmark candidate | A local repository you allowed |
| Available by default | Yes | **No** |
| Repository code executed | No | No |
| Behavioral verification | Yes, against declared cases | **Not possible** |
| Decision it can reach | `ACCEPT` or `REJECT` | `INCONCLUSIVE` only |
| Can restore a baseline | Yes, from the benchmark workspace | No, and it never tries |

### Why real mode cannot accept a migration

UPSHIFT does not execute a repository, so it cannot observe a repository's
behavior. Behavioral verification compares *observed* behavior against *declared
expected* behavior; with no observation, there is nothing to compare, and no
honest way to declare preserved behavior.

So real-repository mode attaches no expected behavior to any case. The existing
verifier reports every case `INCONCLUSIVE`, and the existing decision engine
therefore reports `INCONCLUSIVE`. This is the correct result, not a gap to be
papered over: a repository UPSHIFT only reads is never reported as safe to
migrate.

What you *do* get is real static evidence, from the same engines the benchmark
uses: which files reference a symbol, how large the blast radius is, what the
risk factors are, and an explicit record of what was and was not read.

## Turning it on

The boundary is **closed by default**. Nothing is readable until an operator
names a directory:

```bash
.venv\Scripts\python.exe -m app.api --allow-repository-root C:\src\billing-service
```

The flag is repeatable, for more than one root:

```bash
.venv\Scripts\python.exe -m app.api \
    --allow-repository-root C:\src\billing-service \
    --allow-repository-root C:\src\payments-service
```

A repository is readable only when its **resolved** path is inside an allowed
root. An allowed root that is not an existing absolute local directory stops the
server at start-up rather than being ignored.

Omit the flag and the dashboard still serves the controlled benchmark; the
real-repository option is shown as unavailable with the reason.

## The boundary

The boundary is the whole security story, so it is worth being precise.

* **It is operator configuration, not request input.** Only the process that
  starts the server can widen it. `create_dashboard_app` resolves the roots once,
  stores them as an immutable tuple on the application, and no request can add
  to or replace them.
* **Containment is checked on the resolved path.** `..` segments, symbolic links,
  and Windows junctions cannot carry a path outside the boundary; a traversal
  attempt fails exactly like any other outside path.
* **A refusal never echoes the value.** The message says what was refused and
  where the rule is, never the path or the declaration that was refused, so the
  endpoint cannot be used to probe the filesystem.
* **There is no discovery.** No default root, no search, no environment
  variable, no `~` expansion, no last-used path.
* **A request cannot combine the two modes.** `POST /api/analyze` accepts exactly
  one of two shapes, never a blend.

## The read

`app/repository/real_input.py` walks the validated root once, breadth-first,
in sorted order, and re-checks containment for every single entry it is about
to touch.

It will not:

* follow a symbolic link or a Windows junction;
* read a credential, private key, token, or environment file, by name or by
  extension;
* read a file type the Phase 4 analyzer cannot search;
* read a file over 262,144 bytes;
* read more than 2,000 files, 33,554,432 bytes, or 4,000 directories per run;
* walk a generated, cached, or version-control directory;
* execute, import, compile, or evaluate anything it read;
* write, rename, or delete anything.

Content is read as text and treated as text. Binary content is detected, not
guessed at.

### Evidence, not a claim

The result reports exactly what the read covered: which files were inspected,
which were skipped, and the reason for each skip with an exact count. A skip is
never hidden by omission; where a list is capped for size, the cap is reported
as truncated.

The result never contains an absolute filesystem path. It shows the repository
name, repository-relative labels, the limits that were applied, and the
restrictions that were enforced.

## Why the analyzed repository is never written

The Phase 4 impact analyzer is a protected file: the project freezes it and
`tests/test_recovery_engine.py` asserts its digest. It discovers references by
walking a directory.

Rather than modify a frozen engine or reimplement its rules, real-repository mode
copies the **already-bounded** read into a private scratch directory of UPSHIFT's
own and points the unchanged analyzer at that. This means:

* the reference-discovery rules are literally the same code the benchmark uses, so
  the two modes cannot drift apart;
* the analyzer can only see files the reader admitted, so it cannot reach a
  refused file or exceed a read budget;
* your repository is opened read-only and is unchanged when the analysis ends.

The scratch copy is removed when the analysis ends, including when it fails. It
is the same pattern the controlled benchmark already uses for its rollback
workspace.

The copy is written **byte-for-byte**. Newline translation is explicitly disabled
when the snapshot is written, because the platform default rewrites every `\n`
as `os.linesep` and would turn a repository file's `\r\n` into `\r\r\n`. The
analyzer's universal-newline read would then see a blank line at every original
line break, so every reported line number after the first would be shifted and
every line count inflated. The evidence has to describe the repository, not
UPSHIFT's copy of it.

## The migration declaration

Real-repository mode needs to know what is being migrated. The declaration is
**data**, and it has no field that can name a file to read, a module to import,
or a command to run:

```json
{
  "repository_path": "C:\\src\\billing-service",
  "migration": {
    "name": "Billing directory migration",
    "old_api": "legacy_pkg.old_call",
    "target_api": "modern_pkg.new_call",
    "renamed_symbols": [["old_call", "new_call"]]
  }
}
```

`old_symbols` and `target_symbols` are optional. When omitted, the existing
Phase 4 engine derives them from the entry points, exactly as it does for the
controlled benchmark, so both modes search with one vocabulary rule. A symbol
must be a dotted identifier; a path fragment, a shell word, or a string with a
control character is refused.

An unexpected field is refused rather than ignored, so a declaration cannot
smuggle an extra parameter past review.

## HTTP

The route surface is unchanged: three routes, no fourth.

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | The dashboard document |
| `/api/candidates` | GET | Candidates, service boundary, and both analysis modes |
| `/api/analyze` | POST | Run one analysis |

`POST /api/analyze` accepts exactly one of:

```json
{"candidate_id": "correct_migration"}
```

```json
{"repository_path": "/abs/path", "migration": {"name": "...", "old_api": "...", "target_api": "..."}}
```

Anything else is a 400. That includes a `candidate_id` *and* a `repository_path`,
a `repository_path` with no `migration`, and any extra field.

The 4,096-byte body cap is unchanged. A very long repository path is refused with
413 rather than admitted.

`GET /api/candidates` reports both modes, including whether a boundary is
configured, without disclosing a path.

## Recovery in this mode

A repository UPSHIFT only reads has no controlled baseline: nothing was migrated,
so there is no prior state to restore. `NoControlledRollbackProvider` reports that
mechanism as unavailable, which makes the outcome structural rather than
incidental.

In practice the existing recovery engine never reaches a rollback attempt here.
Verification is `INCONCLUSIVE` by design, so the engine already records that
neither acceptance nor rollback is justified, and it says so in its explanations.

## How the read is proven, not asserted

The most important property of this mode is not that it is careful, but that it
is *real*: the impact evidence must come from the bytes on disk. A migration
tool that reported a confident blast radius it had not derived would be worse
than one that reported nothing.

`tests/test_real_repository_evidence.py` pins that directly, and every
expectation in it is derived from content the test itself writes:

* a symbol is planted at a known line, and that line is **recomputed from the
  written bytes** before the assertion, so a stale or memorized line number
  fails;
* one declaration is run against **two different repositories** holding
  different symbols, and each must report only its own;
* a repository is analyzed, **mutated, and analyzed again**, and the results
  must differ in the specific way the mutation predicts - a call site added, a
  symbol removed, a file added, a file deleted, bytes changed;
* a symbol that is declared but **absent** from the files must not be reported,
  which distinguishes a search from an echo of the declaration.

No expected analysis is hardcoded in the product, and no test reads a real user's
source tree. As a direct check: if the reader is replaced with one that returns
a complete, successful, empty read, every discovered reference, every impacted
file, and every per-symbol verification case disappears. There is nothing left
to display, because there was never anything cached.

### Two defects this found

Both were found by these tests and are fixed. They are recorded because a
boundary-only test suite did not catch either one.

| Defect | Effect | Fix |
| --- | --- | --- |
| The snapshot bridge wrote with platform newline translation | A CRLF file - the norm on Windows - was stored as CR CR LF, so every reported line number after the first was shifted and every line count was inflated | Write with `newline=""` so the snapshot is byte-identical to what was read |
| The reader compared an unresolved root against resolved entries | Containment failed for every entry, so a root supplied in an equivalent short form (`C:\Users\BHIMAV~1\...`, which is what `tempfile` yields) produced a **successful, empty read** - indistinguishable in the evidence from a repository with nothing relevant in it | Resolve the root once at construction, so containment compares like with like |

The second failed in the safe direction - it read nothing rather than too much -
and the boundary always supplies an already-resolved root, so it was latent
rather than reachable from the dashboard. It was still a wrong answer, and a
silently empty one, which is the specific failure mode this mode must not have.

## Limitations

These are real limits, stated plainly.

* **No behavioral evidence.** The consequence of not executing code is that a
  real repository can never be accepted. Do not read an `INCONCLUSIVE` decision
  as a soft pass; it is a refusal to guess.
* **Textual analysis only.** Symbol search is textual. A reference assembled at
  runtime, or a name reached through reflection, will not be found. Impact
  evidence can therefore understate a real blast radius.
* **Bounded by design.** The read budgets mean a large repository is analyzed
  partially. The evidence reports how much, so a partial read is visible rather
  than silent.
* **The extension allowlist is narrow.** Only the extensions the Phase 4 analyzer
  can search are read, and the reader does not widen it.
* **Relevance is decided by file type, not by the migration.** Every text file
  with an analyzable extension inside the boundary is read, because a reference
  can be anywhere and the analyzer searches text. The read stays bounded by byte,
  file, and directory budgets, but it is not narrowed to the files the declared
  symbols turn out to live in. Narrowing it would require reading the files
  first, so the saving would be illusory.
* **No dependency resolution.** Files outside the boundary are never read, so a
  reference that only appears in a sibling checkout, a vendored archive, or a
  lockfile-adjacent manifest is out of scope.
* **No link traversal.** A reference that only exists behind a symbolic link is
  out of scope by policy.
* **Single-machine, local-only.** Loopback binding, no network access, no Git
  invocation, no remote repository support.

## Focused tests

```bash
.venv\Scripts\python.exe -m pytest tests/test_repository_input.py tests/test_real_repository_evidence.py -q
```

Two suites, with different jobs:

`tests/test_repository_input.py` holds the **boundary**. It asserts the closed
default, containment and traversal refusal, non-echoing refusals, the reader's
policies, determinism, the honesty of the `INCONCLUSIVE` result, the three-route
surface, and that the frozen Phase 4 analyzer is still byte-for-byte the locked
file.

`tests/test_real_repository_evidence.py` holds the **proof of a real read**. It
derives every expectation from bytes it writes, as described above.

Both build every fixture in a temporary directory. Neither reads a real user's
source tree.

## The MCP bridge

The same real-repository evidence is reachable through the local MCP server, so
a coding agent can ask for it without a human wiring anything per call. The
bridge adds no reader, no pipeline, and no second set of engines: the tool calls
`app.api.repository_service.analyze_repository`, which is the function the HTTP
route already used, and projects the result it returns.

### Approving a repository

An operator lists directories in `.upshift/mcp-repository.json`:

```json
{
  "allowed_repository_roots": ["../customer-service", "C:/work/legacy-billing"]
}
```

Each entry is resolved (relative entries against the project root) and then
validated by the existing `build_repository_boundary`, which requires an
absolute, existing local directory. The file is optional: with no file there is
a **closed** boundary, the server stays in controlled-benchmark mode, and any
real-repository request is refused. A malformed file fails loudly instead of
degrading silently, so a broken approval is never mistaken for an empty one.

A configuration file is used rather than an environment variable because
`upshift_mcp` declares no environment access at all, and keeping that ban
intact is worth more than the convenience of `export`. The trade-off is that an
approval becomes reviewable in a diff instead of hidden in a shell profile.

### Calling it

```json
{
  "repository": "customer-service",
  "migration": {
    "name": "<migration name>",
    "old_api": "<old entry point>",
    "target_api": "<target entry point>"
  }
}
```

The declaration fields are deliberately left as placeholders here. Writing a
concrete entry point into this file would make the document an impacted file of
the controlled benchmark and move its frozen Phase 4 evidence;
`demo/migration_benchmark/migration_metadata.json` is the concrete example.

`repository` is the **name** of an approved root, never a path. The server
matches it literally against the approved names and then re-checks the resolved
root against the same boundary, so a caller can narrow the choice but can never
introduce a root, traverse out of one, or follow a link out of one. With several
roots approved, a name is required. `migration` is the existing declaration
validator, which reduces every field to dotted identifiers, so a declaration
cannot smuggle a path or a command.

Passing neither argument keeps the controlled benchmark exactly as it was.

### What comes back

`mode` is `real_repository` and the payload adds `repository` (inspected paths,
skipped files with reasons, the read limits actually applied), `approved_repositories`,
and `limitations`. The `impact`, `risk`, `verification`, `decision`, and
`recovery` sections are the same ones the dashboard shows. The absolute location
is still absent, exactly as it is over HTTP: a result never carries a filesystem
path, only the repository's name and repository-relative labels.

The result is still `INCONCLUSIVE`, and now says why in `limitations`: the
repository was read and never run, so UPSHIFT cannot observe behaviour and will
not report a real repository as accepted.

### Focused tests

```bash
.venv\Scripts\python.exe -m pytest tests/test_mcp_real_repository.py -q
```

The suite covers the closed default, reading an approved temporary repository,
dynamic evidence as files are edited, added, and removed, refusal of anything
outside the boundary, `..` traversal, symlink and Windows-junction escapes, the
preserved size and extension limits, proof that repository contents are never
executed and never modified, unchanged benchmark behaviour, the limited tool
surface, and the same checks again over a real STDIO session.
