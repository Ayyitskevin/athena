"""Athena's MCP (Model Context Protocol) surface.

This package lets an AI agent drive Athena through MCP tools. It is OPTIONAL: it
ships behind the `mcp` extra (`pip install athena[mcp]`) and stays out of the
core app's import graph, so the base install stays lean — the one exception is
`config` (stdlib-only; the launch-contract shape that agent onboarding returns).
Importing this package does NOT pull in the MCP SDK — `client` needs only httpx;
`server` (which imports the SDK) is imported only when you actually run the
server.
"""
