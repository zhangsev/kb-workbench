"""Real integration verification. Run AFTER `kbw setup --examples` and `kbw index`:

    python scripts/verify_real.py

Talks to the real LLMWiki and CodeGraph MCP servers (no stubs), drives the gateway
through a real MCP client, and writes docs/VERIFY-RESULTS.md. A step that cannot run
is recorded as FAILED with the error; nothing is assumed to pass.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from kbw.code import parse_explore  # noqa: E402
from kbw.config import load_config  # noqa: E402
from kbw.gateway import Gateway  # noqa: E402

RESULTS: list[dict] = []


def children(pid: int) -> list[dict]:
    """Direct child processes of `pid` (no third-party packages)."""
    if os.name == "nt":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    else:
        out = subprocess.run(["ps", "-o", "pid=,comm=,args=", "--ppid", str(pid)],
                             capture_output=True, text=True).stdout
        out = "\n".join("\t".join(line.split(None, 2)) for line in out.splitlines())
    rows = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if parts and parts[0].isdigit():
            rows.append({"pid": int(parts[0]), "name": parts[1] if len(parts) > 1 else "",
                         "cmd": parts[2] if len(parts) > 2 else ""})
    return [r for r in rows if "powershell" not in r["name"].lower() and r["name"] != "ps"]


def kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        os.kill(pid, 9)


async def step(name: str, fn) -> None:
    t0 = time.perf_counter()
    try:
        detail = await fn()
        RESULTS.append({"step": name, "status": "PASS", "ms": round((time.perf_counter() - t0) * 1000), "detail": detail})
    except BaseException as exc:  # record and continue
        def describe(error):
            subs = getattr(error, "exceptions", None)
            if subs:
                return "; ".join(describe(e) for e in subs)
            return "".join(traceback.format_exception(error))
        msg = describe(exc)
        RESULTS.append({"step": name, "status": "FAILED", "ms": round((time.perf_counter() - t0) * 1000), "detail": msg})
    print(f"[{RESULTS[-1]['status']}] {name} ({RESULTS[-1]['ms']} ms)", flush=True)


def ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


async def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    gw = Gateway(cfg)
    repo = cfg.repos[0]

    async def llmwiki_direct():
        t = time.perf_counter()
        status = await gw.wiki.call("wiki_status")
        cold = ms(t)
        t = time.perf_counter()
        q = await gw.wiki.call("wiki_query", {"query": "订单 超时"})
        warm = ms(t)
        data = q["json"] or q["structured"] or {}
        assert data.get("results"), f"wiki_query returned no results: {q['text'][:300]}"
        first = data["results"][0]
        page = await gw.wiki.call("wiki_read_page", {"path": first["path"]})
        assert page["text"], "wiki_read_page returned nothing"
        return {"tools": gw.wiki.stats.tools, "cold_ms": cold, "warm_ms": warm, "status": status["json"],
                "first_result": first, "read_page_chars": len(page["text"])}

    async def llmwiki_write_blocked():
        try:
            await gw.wiki.call("wiki_write_page", {"path": "x.md", "content": "x"})
        except PermissionError:
            return "write tool refused by the gateway allow-list"
        raise AssertionError("write tool was not blocked")

    async def codegraph_direct():
        t = time.perf_counter()
        res = await gw.codegraph.call("codegraph_explore", {"query": "OrderService cancelOrder", "projectPath": str(repo.path)})
        cold = ms(t)
        t = time.perf_counter()
        await gw.codegraph.call("codegraph_explore", {"query": "InventoryClient releaseStock", "projectPath": str(repo.path)})
        warm = ms(t)
        sections = parse_explore(res["text"])
        assert sections, "no file sections parsed; output format may differ:\n" + res["text"][:800]
        assert any(s["start"] for s in sections), "no line numbers parsed"
        return {"tools": gw.codegraph.stats.tools, "cold_ms": cold, "warm_ms": warm,
                "sections": [{"path": s["path"], "lines": [s["start"], s["end"]]} for s in sections],
                "raw_head": res["text"][:600]}

    async def codegraph_second_project():
        other = cfg.repos[1]
        res = await gw.codegraph.call("codegraph_explore", {"query": "StockService release", "projectPath": str(other.path)})
        sections = parse_explore(res["text"])
        assert any("StockService" in s["path"] for s in sections), res["text"][:500]
        return {"same_session_starts": gw.codegraph.stats.starts, "paths": [s["path"] for s in sections]}

    async def concurrent_lazy_start():
        await gw.wiki.close(); await gw.codegraph.close()
        w0, c0 = gw.wiki.stats.starts, gw.codegraph.stats.starts
        await asyncio.gather(*[gw.wiki.call("wiki_query", {"query": f"库存 {i}"}) for i in range(8)],
                             *[gw.codegraph.call("codegraph_explore", {"query": f"StockService {i}", "projectPath": str(repo.path)}) for i in range(8)])
        assert gw.wiki.stats.starts == w0 + 1 and gw.codegraph.stats.starts == c0 + 1, (gw.wiki.stats.starts, gw.codegraph.stats.starts)
        return {"llmwiki_starts": 1, "codegraph_starts": 1, "children": children(os.getpid())}

    async def crash_recovery():
        kids = children(os.getpid())
        assert kids, "no child processes found to kill"
        for k in kids:
            kill(k["pid"])
        await asyncio.sleep(1)
        t = time.perf_counter()
        r1 = await gw.wiki.call("wiki_query", {"query": "库存"})
        r2 = await gw.codegraph.call("codegraph_explore", {"query": "StockService", "projectPath": str(repo.path)})
        assert r1["text"] and r2["text"]
        return {"killed": [k["pid"] for k in kids], "recovered_in_ms": ms(t),
                "new_children": [k["pid"] for k in children(os.getpid())]}

    async def gateway_over_mcp():
        env = dict(os.environ, KBW_CONFIG=str(cfg.file), PYTHONPATH=str(ROOT))
        params = StdioServerParameters(command=sys.executable, args=["-m", "kbw", "serve"], env=env)
        out = {}
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)

                async def call(tool, **args):
                    t = time.perf_counter()
                    res = await s.call_tool(tool, args)
                    data = json.loads(res.content[0].text)
                    assert "error" not in data, data
                    return data, ms(t)

                kn, out["search_knowledge_cold_ms"] = await call("search_knowledge", query="订单超时取消的规则")
                top = kn["results"][0]
                assert top["page"] == "concepts/order-timeout-cancel.md", top["page"]
                out["knowledge_routes"], out["knowledge_degraded"] = kn["routes_used"], kn["degraded"]
                _, out["search_knowledge_warm_ms"] = await call("search_knowledge", query="库存预占泄漏怎么排查")
                page, out["read_page_ms"] = await call("read_page", path=top["page"])
                out["read_page_served_by"] = page["served_by"]
                src = top["provenance"]["sources"][0]["source_path"]
                raw, out["read_source_ms"] = await call("read_source", path=src)
                assert "30 分钟" in raw["content"]
                out["provenance_chain"] = f'{top["page"]} -> {top["provenance"]["sources"][0]["via"]} -> {src}'

                code, out["search_code_cold_ms"] = await call("search_code", query="OrderService cancelOrder 怎么释放库存")
                hit = code["results"][0]
                for field in ("repo", "commit", "path", "lines", "index_freshness"):
                    assert hit.get(field), f"missing {field}: {hit}"
                out["code_top"] = {k: hit[k] for k in ("repo", "commit", "path", "lines", "index_freshness", "route_ranks")}
                out["code_degraded"] = code["degraded"]
                _, out["search_code_warm_ms"] = await call("search_code", query="StockService reserve 实现")
                cached, out["search_code_cached_ms"] = await call("search_code", query="StockService reserve 实现")
                assert cached["cache"]["hit"] is True
                cross, _ = await call("search_code", query="order.cancelled 消息下游谁消费")
                out["cross_service_repos"] = [q["repo"] for q in cross["repos_queried"]]
                assert len(out["cross_service_repos"]) >= 2, cross["repos_queried"]
                both, out["search_all_ms"] = await call("search_all", query="库存预占泄漏 releaseStock")
                out["search_all_kinds"] = sorted({r["kind"] for r in both["results"]})
                st, _ = await call("service_status")
                out["downstream_states"] = {k: v["state"] for k, v in st["downstream"].items()}
                out["models"] = st["models"]
        return out

    async def clean_shutdown():
        await gw.close()
        await asyncio.sleep(2)
        left = children(os.getpid())
        assert not left, f"child processes left after close: {left}"
        return "no child process left; CodeGraph's own shared daemon (if it started one) is intentionally not touched"

    await step("LLMWiki real MCP call (wiki_status, wiki_query, wiki_read_page)", llmwiki_direct)
    await step("LLMWiki write tools blocked by allow-list", llmwiki_write_blocked)
    await step("CodeGraph real MCP call (codegraph_explore)", codegraph_direct)
    await step("CodeGraph second project through the same session (projectPath)", codegraph_second_project)
    await step("Concurrent lazy start launches one process per downstream", concurrent_lazy_start)
    await step("Recovery after downstream processes are killed", crash_recovery)
    await step("Gateway end to end over MCP (knowledge -> page -> source, code, cross-service, cache)", gateway_over_mcp)
    await step("Clean shutdown leaves no child process", clean_shutdown)

    head = (f"# Real integration verification\n\n- date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
            f"- platform: {platform.platform()}\n- python: {sys.version.split()[0]}\n- config: {cfg.file}\n\n"
            "Timings are single measurements on this machine, not guarantees.\n\n| step | status | ms |\n|---|---|---|\n")
    body = "".join(f"| {r['step']} | {r['status']} | {r['ms']} |\n" for r in RESULTS)
    detail = "\n".join(f"## {r['step']}\n\n```json\n{json.dumps(r['detail'], ensure_ascii=False, indent=2, default=str)}\n```\n" for r in RESULTS)
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "VERIFY-RESULTS.md").write_text(head + body + "\n" + detail, encoding="utf-8")
    failed = [r for r in RESULTS if r["status"] != "PASS"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed. Report: docs/VERIFY-RESULTS.md")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
