"""A real MCP stdio server (official SDK) used to test connection management.

It is NOT a mock of LLMWiki or CodeGraph: it exists so that lazy start,
single-flight, timeout, crash recovery and shutdown are exercised over the real
MCP protocol and a real child process. Each start appends its pid to the file
named by FAKE_PID_LOG.
"""
import asyncio
import os

from mcp.server.mcpserver import MCPServer

with open(os.environ["FAKE_PID_LOG"], "a", encoding="utf-8") as fh:
    fh.write(f"{os.getpid()}\n")

server = MCPServer("fake-downstream")


@server.tool()
async def echo(text: str) -> str:
    return f"{os.getpid()}:{text}"


@server.tool()
async def slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "done"


@server.tool()
async def crash() -> str:
    os._exit(3)


@server.tool()
async def write_thing() -> str:
    return "should never be reachable through the allow-list"


if __name__ == "__main__":
    server.run("stdio")
