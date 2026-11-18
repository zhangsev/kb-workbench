import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kbw import examples
from kbw.code import parse_explore
from kbw.config import load_config
from kbw.gateway import Gateway
from kbw.util import extract_symbols, fts_tokens, rrf

ROOT = Path(__file__).resolve().parent.parent
STUB = str(Path(__file__).with_name("stub_codegraph.py"))


@pytest.fixture()
def project(tmp_path):
    """Demo repos + demo knowledge. LLMWiki disabled; CodeGraph absent unless `stub`."""
    cfg_path = tmp_path / "kbw.yaml"
    examples.create(tmp_path, cfg_path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["downstream"] = {"llmwiki": {"enabled": False},
                         "codegraph": {"command": "kbw-codegraph-not-installed"}}
    raw["timeouts"] = {"idle_seconds": 0}
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return tmp_path


def use_stub(project):
    cfg_path = project / "kbw.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["downstream"]["codegraph"] = {"command": sys.executable, "args": [STUB]}
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    for name in ("order-service", "inventory-service"):
        d = project / "examples" / "repos" / name / ".codegraph"
        d.mkdir()
        (d / "codegraph.db").write_bytes(b"stub")
        (d / ".gitignore").write_text("*\n")


def test_units():
    assert "超时" in fts_tokens("订单超时取消") and "order" in fts_tokens("OrderService")
    kinds = {s["text"]: s["kind"] for s in extract_symbols(
        "OrderService.cancelOrder 调 /api/inventory/release 发 order.cancelled 配置 order.timeout-minutes")}
    assert kinds["OrderService"] == "class" and kinds["/api/inventory/release"] == "path"
    assert "order.cancelled" in kinds and "cancelOrder" in kinds
    fused = rrf({"a": ["x", "y"], "b": ["y", "z"]})
    assert fused[0][0] == "y" and fused[0][2] == {"a": 2, "b": 1}
    secs = parse_explore("Found 1\n\n**`src/A.java`** — A(class)\n\n3\tclass A {\n4\t}\n\n**Not shown above — x**\n- B")
    assert secs == [{"path": "src/A.java", "symbols": "A(class)", "body": ["", "3\tclass A {", "4\t}", ""],
                     "start": 3, "end": 4}]


def test_end_to_end_over_mcp_degraded(project):
    """Real MCP client -> real `python -m kbw serve` process. No downstream, no models:
    everything must still answer, and say exactly what is degraded."""
    outside = project / "secret.txt"
    outside.write_text("top secret")
    link = project / "examples" / "kb" / ".wiki" / "raw" / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None

    async def go():
        env = dict(os.environ, KBW_CONFIG=str(project / "kbw.yaml"), PYTHONPATH=str(ROOT))
        params = StdioServerParameters(command=sys.executable, args=["-m", "kbw", "serve"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = sorted(t.name for t in (await s.list_tools()).tools)
                assert names == ["check_knowledge", "find_knowledge", "organize_knowledge", "read_page", "read_source",
                                 "reindex_knowledge", "save_knowledge", "search_all", "search_code",
                                 "search_knowledge", "service_status"]

                async def call(tool, **args):
                    res = await s.call_tool(tool, args)
                    return json.loads(res.content[0].text)

                kn = await call("search_knowledge", query="订单超时取消的规则")
                top = kn["results"][0]
                assert top["page"] == "concepts/order-timeout-cancel.md" and top["lines"][0]
                assert any(d.startswith("semantic") for d in kn["degraded"])
                assert any(d.startswith("wiki_query") for d in kn["degraded"])
                src = top["provenance"]["sources"][0]
                assert src == {"via": "sources/order-timeout-design-summary.md",
                               "source_path": "raw/order-timeout-design.md"}
                assert top["provenance"]["updated"] == "2026-05-21"

                page = await call("read_page", path=top["page"])
                assert "order.timeout-minutes" in page["content"] and page["served_by"] == "local_file"
                raw = await call("read_source", path=src["source_path"])
                assert "30 分钟未支付" in raw["content"]

                # provenance that does not exist is reported, not invented
                inv = (await call("read_page", path="entities/inventory-service.md"))["provenance"]
                assert inv["sources"] == [] and inv["updated"] is None
                assert "page declares no sources" in inv["missing"]

                for bad in ("../wiki/index.md", "../../../secret.txt", str(outside)):
                    assert (await call("read_source", path=bad))["error"] in ("PermissionError", "FileNotFoundError")
                if link is not None:
                    assert (await call("read_source", path="link.txt"))["error"] == "PermissionError"
                assert (await call("read_page", path="../raw/order-timeout-design.md"))["error"] == "PermissionError"

                code = await call("search_code", query="OrderService cancelOrder 在哪里")
                hit = next(r for r in code["results"] if r["path"].endswith("OrderService.java"))
                assert hit["repo"] == "order-service" and len(hit["commit"]) == 40
                assert hit["index_freshness"] == "not_indexed" and hit["exact_hits"][0]["line"] > 0
                assert any("no CodeGraph index" in d for d in code["degraded"])
                assert any("exact text search only" in d for d in code["degraded"])

                st = await call("service_status")
                assert st["downstream"]["llmwiki"]["state"] == "unavailable"
                assert st["models"]["embedding"]["state"] in ("unavailable", "not_loaded")
                both = await call("search_all", query="库存预占泄漏 releaseStock")
                assert {r["kind"] for r in both["results"]} == {"knowledge", "code"}
    asyncio.run(go())


def test_routing_expansion_cross_service_cache(project):
    use_stub(project)

    async def go():
        gw = Gateway(load_config(str(project / "kbw.yaml")))
        try:
            # concurrent first calls start exactly one downstream process
            many = await asyncio.gather(*[gw.search_code(f"StockService reserve {i}") for i in range(8)])
            assert gw.codegraph.stats.starts == 1 and all(m["results"] for m in many)

            r = await gw.search_code("库存 StockService release 怎么实现")
            assert r["routing"][0]["repo"] == "inventory-service" and not r["degraded"]
            top = next(x for x in r["results"][:2] if x["path"].endswith("StockService.java"))
            assert top["repo"] == "inventory-service"
            assert top["lines"][0] == 1 and len(top["commit"]) == 40 and top["index_freshness"] == "unknown"
            assert {"codegraph:inventory-service", "ripgrep:inventory-service"} <= set(top["route_ranks"])

            # primary candidate lacks the symbol -> parallel expansion finds it elsewhere
            r = await gw.search_code("订单 下单 OrderCancelledListener")
            assert r["repos_queried"][0]["repo"] == "order-service" and r["expanded"]
            assert any(x["path"].endswith("OrderCancelledListener.java") for x in r["results"])

            # cross-service: related repo is queried even though the primary has results
            r = await gw.search_code("OrderService cancelOrder InventoryClient OrderStatus 之后下游怎么处理")
            assert r["cross_service"] and [q["repo"] for q in r["repos_queried"]] == ["order-service", "inventory-service"]
            assert "cross-service" in r["repos_queried"][1]["reason"]

            # exact cache: hit on repeat, miss after the source changes
            q = "InventoryClient releaseStock"
            assert (await gw.search_code(q))["cache"]["hit"] is False
            assert (await gw.search_code(q))["cache"]["hit"] is True
            f = project / "examples/repos/order-service/src/main/java/com/example/order/InventoryClient.java"
            f.write_text(f.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")
            again = await gw.search_code(q)
            assert again["cache"]["hit"] is False
            assert again["repo_states"]["order-service"]["dirty"] is True

            # knowledge cache invalidates when a page changes; degraded answers are never cached
            k1 = await gw.search_knowledge("库存预占泄漏")
            assert k1["degraded"] and (await gw.search_knowledge("库存预占泄漏"))["cache"]["hit"] is False

            # downstream killed -> next query reconnects
            first = gw.codegraph.stats.starts
            async with gw.codegraph._lock:
                await gw.codegraph._teardown()
            assert (await gw.search_code("StockService 重连"))["results"] and gw.codegraph.stats.starts == first + 1
        finally:
            await gw.close()
    asyncio.run(go())


def test_chinese_filename_and_no_signal_queries(project):
    """Regression for queries reported from real use: Chinese phrases returned
    unrelated files, file names never matched, and 'no signal' picked arbitrary repos."""
    src = project / "examples/repos/inventory-service/src/main/java/com/example/inventory"
    (src / "BusinessSubarrayDelegate.java").write_text(
        "package com.example.inventory;\n\n/**\n * 子阵概览：按子阵汇总设备状态。\n */\n"
        "public class BusinessSubarrayDelegate {\n    public String subarrayView() {\n"
        "        return \"ok\";\n    }\n}\n", encoding="utf-8")
    res = project / "examples/repos/inventory-service/src/main/resources"
    res.mkdir(parents=True)
    (res / "rest-services-device-subarray-topology.yaml").write_text("swagger: '2.0'\ninfo:\n  title: device\n",
                                                                  encoding="utf-8")

    async def go():
        gw = Gateway(load_config(str(project / "kbw.yaml")))
        try:
            for q in ("子阵概览 subarray overview", "子阵概览的接口在哪里实现", "子阵概览"):
                r = await gw.search_code(q)
                top = r["results"][0]
                assert top["repo"] == "inventory-service" and top["path"].endswith("BusinessSubarrayDelegate.java"), q
                assert "子阵概览" in top["matched_terms"] and r["term_mode"] == "phrases"
                # context lines around the hit, not a single line
                assert top["lines"][0] == 1 and top["lines"][1] >= 7 and "public class BusinessSubarrayDelegate" in top["snippet"]
                assert r["routing"][0]["repo"] == "inventory-service" and not r["no_routing_signal"]

            # the phrase carries extra words that are not in the code: fall back to pieces
            r = await gw.search_code("子阵概览页面")
            assert r["term_mode"] == "chinese_pieces"
            assert r["results"][0]["path"].endswith("BusinessSubarrayDelegate.java")

            # a term that only occurs in a file NAME
            r = await gw.search_code("subarray-topology")
            top = r["results"][0]
            assert top["path"].endswith("rest-services-device-subarray-topology.yaml")
            assert top["path_match"] == ["subarray-topology"] and "swagger" in top["snippet"]
            assert any(k.startswith("filename:") for k in top["route_ranks"])

            # nothing matches anywhere: empty, explicit, and no downstream is started to guess
            r = await gw.search_code("完全不存在的业务词 zzzqqqxxx")
            assert r["results"] == [] and r["no_routing_signal"] is True and r["repos_queried"] == []
            assert "kbw.yaml" in r["hint"] and gw.codegraph.stats.starts == 0

            # the caller may still force specific repos
            r = await gw.search_code("完全不存在的业务词 zzzqqqxxx", repos=["order-service"])
            assert [q["repo"] for q in r["repos_queried"]] == ["order-service"] and r["results"] == []

            assert r["index_coverage"] == {"configured": 2, "indexed": 0,
                                           "not_indexed": ["order-service", "inventory-service"]}
            assert (await gw.status())["index_coverage"]["indexed"] == 0
        finally:
            await gw.close()
    asyncio.run(go())


def test_query_command(project):
    import subprocess
    env = dict(os.environ, KBW_CONFIG=str(project / "kbw.yaml"), PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    out = subprocess.run([sys.executable, "-m", "kbw", "query", "code", "StockService 预占"], env=env,
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert "route:   inventory-service" in out.stdout and "StockService.java" in out.stdout
    assert "index:   0/2 repos indexed" in out.stdout
    out = subprocess.run([sys.executable, "-m", "kbw", "index", "--repo", "nope"], env=env,
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 2 and "unknown repos" in out.stdout


def test_encodings_definition_and_single_repo_cost(project, monkeypatch):
    """Chinese in GBK files and in \\uXXXX-escaped .properties must be found;
    a class query returns its defining file first with an outline; a query
    scoped to one repo must not inspect the others."""
    inv = project / "examples/repos/inventory-service"
    (inv / "src/main/java/com/example/inventory/LegacyGbk.java").write_bytes(
        "package com.example.inventory;\n// 逆变器告警屏蔽规则\npublic class LegacyGbk {}\n".encode("gbk"))
    res = inv / "src/main/resources"
    res.mkdir(parents=True, exist_ok=True)
    escaped = "".join(f"\\u{ord(c):04X}" for c in "组串离散率")
    (res / "messages_zh_CN.properties").write_text(f"string.dispersion={escaped}\n", encoding="ascii")

    import kbw.code as code_mod
    git_calls, rg_calls = [], []
    real_git = code_mod.git_state
    monkeypatch.setattr(code_mod, "git_state", lambda path: (git_calls.append(Path(path).name), real_git(path))[1])

    async def go():
        gw = Gateway(load_config(str(project / "kbw.yaml")))
        real_pass = gw.code._rg_pass
        gw.code._rg_pass = lambda repo, *a, **k: (rg_calls.append(repo.name), real_pass(repo, *a, **k))[1]
        try:
            r = await gw.search_code("逆变器告警屏蔽规则")
            top = r["results"][0]
            assert top["path"].endswith("LegacyGbk.java") and "逆变器告警屏蔽规则" in top["snippet"]  # decoded, readable

            r = await gw.search_code("组串离散率")
            assert r["results"][0]["path"].endswith("messages_zh_CN.properties")
            assert r["results"][0]["matched_terms"] == ["组串离散率"]

            r = await gw.search_code("StockService 是做什么的")
            top = r["results"][0]
            assert top["path"].endswith("StockService.java") and top["definition_of"] == "StockService"
            assert "public class StockService" in top["outline"]
            assert "public synchronized boolean reserve(String sku, int quantity)" in top["outline"]
            assert any(k.startswith("definition:") for k in top["route_ranks"])

            # scoped to one repo: no git check and no search in the other one, one content pass
            git_calls.clear(); rg_calls.clear()
            r = await gw.search_code("releaseStock", repos=["order-service"])
            assert r["results"] and {x["repo"] for x in r["results"]} == {"order-service"}
            assert set(git_calls) == {"order-service"} and rg_calls == ["order-service"]

            # unscoped: each repo is searched once, not once for routing and again for results
            rg_calls.clear()
            await gw.search_code("reserveStock")
            assert sorted(rg_calls) == ["inventory-service", "order-service"]
            assert all("_hits" not in x for x in r["routing"])
        finally:
            await gw.close()
    asyncio.run(go())
