"""MCP gateway: turn admin-configured MCP servers into registered agent tools.

This package owns the outbound integration; it adapts the asyncio MCP SDK to
the synchronous tool registry without leaking async into the runtime core.
"""
