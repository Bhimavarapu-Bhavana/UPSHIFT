"""UPSHIFT local MCP integration, served over local STDIO transport.

This package was originally reserved as ``mcp`` during the Phase 1 scaffold.
It was renamed to ``upshift_mcp`` because the official MCP Python SDK is also
imported as ``mcp``; while the repository root was on ``sys.path`` the reserved
placeholder shadowed the installed SDK and made ``import mcp.server`` fail.
The rename removes that namespace collision without changing the scaffold's
meaning or the local, STDIO-only integration boundary.

This package is deliberately import-cheap: importing it does not import the
MCP SDK and does not start a server.
"""
