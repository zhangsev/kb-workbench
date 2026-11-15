"""STUB standing in for `codegraph serve --mcp` in unit tests only.

It speaks real MCP and returns text in the section format documented in
kbw/code.py, so routing, expansion, fusion and parsing can be tested without the
upstream binary. It proves nothing about CodeGraph itself: the real integration
check is `scripts/verify_real.py`, run on a machine with CodeGraph installed.
"""
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("stub-codegraph")


@server.tool()
async def codegraph_explore(query: str, projectPath: str, maxFiles: int = 5) -> str:
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query))
    out = []
    for path in sorted(Path(projectPath).rglob("*.java")):
        text = path.read_text(encoding="utf-8")
        hits = [w for w in words if w in text]
        if not hits:
            continue
        rel = path.relative_to(projectPath).as_posix()
        body = "\n".join(f"{i}\t{line}" for i, line in enumerate(text.splitlines(), 1))
        out.append((len(hits), f"**`{rel}`** — {', '.join(sorted(hits))}\n\n{body}"))
    out.sort(key=lambda x: -x[0])
    return "\n\n".join(s for _, s in out[:maxFiles]) or "No matching symbols."


if __name__ == "__main__":
    server.run("stdio")
