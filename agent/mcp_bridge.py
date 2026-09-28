"""One bounded MCP client operation in an isolated subprocess (official SDK v1).

stdin/stdout are JSON. No server-provided text is promoted to system instructions.
"""
import asyncio
from contextlib import AsyncExitStack
import json
import sys
from urllib.parse import urlsplit


async def exchange(request):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    import httpx
    config = request["server"]
    async with AsyncExitStack() as stack:
        if config.get("transport", "stdio") == "stdio":
            params = StdioServerParameters(command=config["command"],
                                           args=config.get("args", []), env=config.get("env", {}),
                                           cwd=config.get("cwd"))
            streams = await stack.enter_async_context(stdio_client(params))
        elif config["transport"] == "streamable-http":
            url = urlsplit(config["url"])
            if url.username or url.password or url.query or url.fragment or not url.hostname:
                raise ValueError("invalid MCP endpoint")
            if url.scheme != "https" and not (url.scheme == "http" and url.hostname in
                                               ("localhost", "127.0.0.1", "::1")):
                raise ValueError("remote MCP endpoints require HTTPS")
            http = await stack.enter_async_context(httpx.AsyncClient(
                headers=config.get("headers", {}), timeout=request["timeout"],
                follow_redirects=False, trust_env=False))
            streams = await stack.enter_async_context(streamable_http_client(
                config["url"], http_client=http))
        else:
            raise ValueError("unsupported MCP transport")
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        tools = []
        cursor = None
        for _ in range(8):
            page = await session.list_tools(cursor=cursor)
            tools.extend(tool.model_dump(mode="json", exclude_none=True) for tool in page.tools)
            if len(tools) > 128:
                raise ValueError("MCP catalog exceeds 128 tools")
            cursor = page.nextCursor
            if not cursor:
                break
        else:
            raise ValueError("MCP catalog pagination limit exceeded")
        if request["action"] == "list":
            return {"success": True, "tools": tools}
        name = request["tool"]
        if name not in config.get("allowed_tools", []):
            return {"success": False, "error": "tool is not authorized"}
        tool = next((tool for tool in tools if tool["name"] == name), None)
        if tool is None:
            return {"success": False, "error": "tool no longer exists on this server"}
        from jsonschema import Draft202012Validator
        schema = tool["inputSchema"]
        # Never resolve schemas through network references supplied by a server.
        reject_remote_refs(schema)
        Draft202012Validator(schema).validate(request["arguments"])
        result = await session.call_tool(name, arguments=request["arguments"])
        data = result.model_dump(mode="json", exclude_none=True)
        return {"success": not bool(result.isError), "result": data,
                "untrusted_external_content": True}


def reject_remote_refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("$ref", "$dynamicRef") and (not isinstance(child, str) or not child.startswith("#")):
                raise ValueError("only local JSON schema references are supported")
            reject_remote_refs(child)
    elif isinstance(value, list):
        for child in value:
            reject_remote_refs(child)


def main():
    try:
        raw = sys.stdin.buffer.read(262145)
        if len(raw) > 262144:
            raise ValueError("request too large")
        request = json.loads(raw)
        result = asyncio.run(asyncio.wait_for(exchange(request), timeout=request["timeout"]))
        encoded = json.dumps(result, ensure_ascii=True)
        if len(encoded) > 65536:
            result = {"success": False, "error": "MCP response exceeds 64 KB; narrow the request"}
    except ModuleNotFoundError:
        result = {"success": False, "error": "MCP dependencies missing; install requirements-external.txt"}
    except Exception as exc:
        # SDK exception groups may contain tokens, headers and command arguments.
        result = {"success": False, "error": "MCP operation failed", "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
