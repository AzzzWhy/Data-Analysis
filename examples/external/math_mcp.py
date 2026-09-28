"""Local MCP fixture. Only exposes arithmetic and a deliberately denied test tool."""
from mcp.server.fastmcp import FastMCP
import os
import sys

server = FastMCP("external-math-demo", host="127.0.0.1",
                 port=int(os.environ.get("MCP_DEMO_PORT", "8000")), json_response=True)


@server.tool()
def add(a: float, b: float) -> dict:
    """Add two numeric values; use for exact arithmetic."""
    return {"sum": a + b}


@server.tool()
def restricted_probe() -> dict:
    """Authorization test: must NOT execute unless explicitly allowed by configuration."""
    return {"restricted_tool_executed": True}


if __name__ == "__main__":
    server.run(transport="streamable-http" if "--http" in sys.argv else "stdio")
