# UPSHIFT

UPSHIFT is an autonomous migration-risk and compatibility-proof agent for the IBM BOB 2.0 hackathon. Its purpose is to make a controlled software migration safer by identifying compatibility and regression risks, coordinating approved work through IBM Bob, independently verifying the result, and supporting safe rollback or recovery.

## Current scope

This repository is an initial project scaffold only. It establishes package boundaries, a minimal test baseline, and local development conventions before migration behavior is introduced.

Included now:

- Python package boundaries for the application, security, and verification layers.
- A reserved package for a future local MCP integration.
- A smoke test that confirms the initial packages are importable.
- A focused development dependency for running tests.

Not included yet:

- Migration analysis or execution logic.
- An MCP server or MCP tool definitions.
- IBM Bob connectivity or orchestration.
- Public network services or cloud integrations.
- Credential handling or unrestricted filesystem, shell, or Python execution.

## Architecture boundaries

IBM Bob remains the agentic coding and orchestration layer. UPSHIFT is the migration-risk, compatibility-analysis, verification, and safety layer.

The future MCP integration must begin with narrowly scoped tools over local STDIO transport. It must not expose a public MCP server, arbitrary shell execution, arbitrary Python execution, credential collection, or unrestricted filesystem access. Consequential migration changes require human approval, and a migration is not considered successful until verification passes.

## Repository layout

```text
app/
  api/           Reserved application interfaces
  core/          Reserved core orchestration boundaries
  security/      Reserved safety and approval boundaries
  verification/  Reserved independent verification boundaries
mcp/             Reserved local MCP integration
demo/            Reserved demonstrations
docs/            Reserved project documentation
tests/           Automated checks
.bob/            Reserved IBM Bob workspace
```

## Development

Use a local Python virtual environment, install the test dependency, and run the smoke suite:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python -m pytest
```

No environment file is required for the current scaffold. Configuration placeholders will be introduced only when a concrete, non-sensitive configuration need exists; secrets must remain in ignored local environment files.
