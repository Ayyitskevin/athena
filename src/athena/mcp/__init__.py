"""Athena's MCP (Model Context Protocol) surface.

This package lets an AI agent drive Athena through MCP tools. It is OPTIONAL: it
ships behind the `mcp` extra (`pip install athena[mcp]`) and is never imported by
the core app, so the base install stays lean. Importing this package does NOT pull
in the MCP SDK — `client` needs only httpx; `server` (which imports the SDK) is
imported only when you actually run the server.
"""
