"""Real end-to-end verification of knowledge saving, with the real LLMWiki.

    python scripts/verify_knowledge_save.py

Runs on a temporary COPY of the configured wiki (the real wiki is never touched):
the gateway is started as a real MCP server (`python -m kbw serve`) and driven by
a real MCP client; LLMWiki is started as its own MCP server on the same copy to
check that it reads, indexes and lints the saved pages, and that both writers
respect the shared lock. Writes docs/KNOWLEDGE-VERIFY-RESULTS.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from kbw.config import downstream_env, load_config  # noqa: E402
from kbw.downstream import ManagedSession  # noqa: E402
from kbw.util import resolve_command  # noqa: E402

RESULTS: list[dict] = []


async def step(name, fn):
    t0 = time.perf_counter()
    try:
        detail = await fn()
        status = "PASS"
    except BaseException as exc:  # record and continue
        subs = getattr(exc, "exceptions", None)
        detail = "; ".join(f"{type(e).__name__}: {e}" for e in subs) if subs else f"{type(exc).__name__}: {exc}"
        status = "FAILED"
    RESULTS.append({"step": name, "status": status, "ms": round((time.perf_counter() - t0) * 1000), "detail": detail})
    print(f"[{status}] {name} ({RESULTS[-1]['ms']} ms)", flush=True)


def digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + p.read_bytes())
    return h.hexdigest()


async def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    if not cfg.wiki_root or not cfg.repos:
        print("need knowledge.wiki_root and at least one repo in kbw.yaml")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="kbw-knowledge-verify-"))
    wiki_root = tmp / ".wiki"
    shutil.copytree(cfg.wiki_root, wiki_root, ignore=shutil.ignore_patterns(".llmwiki.lock"))
    pages_dir = wiki_root / "wiki"
    raw_before = digest(wiki_root / "raw") if (wiki_root / "raw").is_dir() else None
    conf = {
        "data_dir": str(tmp / ".kbw"), "offline": cfg.offline,
        "knowledge": {"wiki_root": str(wiki_root), "source_dirs": [str(wiki_root / "raw")]},
        "models": {"dir": str(cfg.model_dir), "embedding": cfg.embedding_model, "rerank_enabled": False},
        "repos": [{"name": r.name, "path": str(r.path), "package_prefixes": r.package_prefixes,
                   "keywords": r.keywords, "depends_on": r.depends_on, "topics": r.topics} for r in cfg.repos],
        "downstream": {"llmwiki": cfg.llmwiki or {}, "codegraph": {"enabled": False}},
        "timeouts": {"idle_seconds": 0, "call_seconds": 60, "start_seconds": 60},
    }
    (tmp / "kbw.yaml").write_text(yaml.safe_dump(conf, allow_unicode=True), encoding="utf-8")
    test_cfg = load_config(str(tmp / "kbw.yaml"))

    # evidence: a real tracked file and the real HEAD of the first repo
    repo = cfg.repos[0]
    files = subprocess.run(["git", "-C", str(repo.path), "ls-files", "*.java"], capture_output=True,
                           text=True, encoding="utf-8").stdout.split()
    files = files or subprocess.run(["git", "-C", str(repo.path), "ls-files"], capture_output=True,
                                    text=True, encoding="utf-8").stdout.split()
    src_file = files[0]
    commit = subprocess.run(["git", "-C", str(repo.path), "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    n_lines = len((repo.path / src_file).read_text(encoding="utf-8", errors="replace").splitlines()) or 1
    ev = {"ref": "s", "repo": repo.name, "path": src_file, "symbol": Path(src_file).stem,
          "lines": [1, min(5, n_lines)], "commit": commit}

    env = dict(os.environ, KBW_CONFIG=str(tmp / "kbw.yaml"), PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    params = StdioServerParameters(command=sys.executable, args=["-m", "kbw", "serve"], env=env)
    spec = resolve_command(*test_cfg.llmwiki_command())
    llm = ManagedSession("llmwiki-verify", spec[0], spec[1], env=downstream_env(), cwd=str(tmp),
                         call_timeout=60, start_timeout=60, idle_timeout=0)
    state: dict = {}
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                async def call(tool, **args):
                    t = time.perf_counter()
                    res = await s.call_tool(tool, args)
                    data = json.loads(res.content[0].text)
                    data["_ms"] = round((time.perf_counter() - t) * 1000, 1)
                    if "error" in data:
                        raise AssertionError(data)
                    return data

                async def save_two():
                    fw = await call("save_knowledge", slug="kbw-verify-framework-usage", kind="framework",
                                    title="校验用框架通用用法", summary="校验用的通用用法页。", services=[repo.name],
                                    keywords=["校验框架", "通用用法"], evidence=[ev],
                                    facts=[{"text": "校验：通用用法事实一", "aspect": "usage", "evidence": ["s"]}],
                                    question="校验框架怎么用", request_id="verify-fw")
                    assert fw["status"] == "created" and fw["searchable"], fw
                    biz = await call("save_knowledge", slug="kbw-verify-business-flow", kind="implementation",
                                     title="校验用业务流程", summary="校验用的业务实现页。", services=[repo.name],
                                     keywords=["校验业务", "入库流程"], evidence=[ev], related=[fw["page_id"]],
                                     facts=[{"text": "校验：业务流程第一步", "aspect": "flow", "evidence": ["s"]}],
                                     open_questions=["校验：是否需要重试"], question="校验业务流程怎么走",
                                     request_id="verify-biz-1")
                    assert biz["status"] == "created" and biz["related_updated"] == [fw["path"]], biz
                    state.update(fw=fw, biz=biz)
                    return {"framework": {k: fw[k] for k in ("page_id", "path", "status", "index", "_ms")},
                            "business": {k: biz[k] for k in ("page_id", "path", "status", "index", "_ms")}}

                async def search_and_read():
                    biz = state["biz"]
                    res = await call("search_knowledge", query="校验业务流程怎么走")
                    top = res["results"][0]
                    assert top["page_id"] == biz["page_id"], res["results"][:3]
                    via_llmwiki = "wiki_query" in top["route_ranks"]
                    page = await call("read_page", path=biz["page_id"])
                    assert page["served_by"] == "llmwiki.wiki_read_page", page["served_by"]
                    assert "校验用业务流程" in json.dumps(page["content"], ensure_ascii=False)
                    assert page["source_check"]["summary"] in ("unchanged", "needs_review")
                    svc = await call("search_knowledge", query="校验", service=repo.name)
                    assert biz["page_id"] in [x["page_id"] for x in svc["results"]]
                    return {"search_ms": res["_ms"], "routes": res["routes_used"],
                            "llmwiki_wiki_query_found_page": via_llmwiki, "read_ms": page["_ms"],
                            "read_served_by": page["served_by"], "source_check": page["source_check"]["summary"],
                            "degraded": res["degraded"]}

                async def three_saves_one_page():
                    biz = state["biz"]
                    cands = await call("find_knowledge", title="校验业务流程", services=[repo.name],
                                       question="校验业务流程第二次")
                    assert cands["candidates"][0]["page_id"] == biz["page_id"], cands
                    r2 = await call("save_knowledge", page_id=biz["page_id"], evidence=[ev],
                                    facts=[{"text": "校验：业务流程第二步", "aspect": "flow", "evidence": ["s"]},
                                           {"text": "校验：业务流程第一步。", "aspect": "flow", "evidence": ["s"]}],
                                    resolve=["校验：是否需要重试"], question="校验业务流程第二次", request_id="verify-biz-2")
                    r3 = await call("save_knowledge", slug="kbw-verify-business-flow", kind="implementation",
                                    title="校验用业务流程", summary="校验用的业务实现页。", services=[repo.name],
                                    keywords=["校验业务"], evidence=[ev],
                                    facts=[{"text": "校验：业务流程第三步", "aspect": "flow", "evidence": ["s"]}],
                                    question="校验业务流程第三次", request_id="verify-biz-3")
                    dup = await call("save_knowledge", slug="kbw-verify-business-flow", kind="implementation",
                                     title="校验用业务流程", summary="x", services=[repo.name], keywords=["x"],
                                     evidence=[ev], facts=[{"text": "x", "evidence": ["s"]}], request_id="verify-biz-3")
                    assert [r2["status"], r3["status"], dup["status"]] == ["updated", "updated", "duplicate_request"]
                    assert r2["path"] == r3["path"] == biz["path"]
                    page = await call("read_page", path=biz["page_id"])
                    k = page["knowledge"]
                    facts = [f["text"] for f in k["facts"]]
                    assert facts == ["校验：业务流程第一步", "校验：业务流程第二步", "校验：业务流程第三步"], facts
                    assert k["open_questions"] == [] and len(k["questions"]) == 3
                    managed = sorted(p.relative_to(pages_dir).as_posix() for p in (pages_dir / "analyses").rglob("*.md"))
                    assert managed == [biz["path"]], managed
                    for q in ("校验业务流程第二次", "校验业务流程第三次"):
                        res = await call("search_knowledge", query=q)
                        assert res["results"][0]["page_id"] == biz["page_id"], q
                    return {"statuses": [r2["status"], r3["status"], dup["status"]], "facts": facts,
                            "questions": k["questions"], "pages": managed}

                async def llmwiki_reads_and_lints():
                    fw, biz = state["fw"], state["biz"]
                    p = await llm.call("wiki_read_page", {"path": biz["path"]})
                    assert "校验用业务流程" in p["text"]
                    idx = await llm.call("wiki_read_index")
                    for path in (biz["path"], fw["path"], f"hubs/{repo.name}.md"):
                        assert path in idx["text"], f"{path} not in LLMWiki index"
                    q = await llm.call("wiki_query", {"query": "校验 业务流程"})
                    lint = await llm.call("wiki_lint")
                    ours = ("analyses/", "frameworks/", "hubs/")
                    data = lint["json"] or lint["structured"] or {}
                    issues = data.get("issues") if isinstance(data, dict) else None
                    if issues is None:
                        issues = data if isinstance(data, list) else []
                    mine = [i for i in issues if any(o in json.dumps(i, ensure_ascii=False) for o in ours)]
                    errors = [i for i in mine if str(i.get("severity", "")).lower() == "error"]
                    assert not errors, errors
                    return {"wiki_query_found": biz["path"] in q["text"], "lint_issues_on_saved_pages": mine,
                            "lint_raw_head": lint["text"][:400] if not issues else None}

                async def shared_lock():
                    biz = state["biz"]
                    entries = await llm.call("wiki_list_pages")
                    target = next((x for x in ("concepts/order-timeout-cancel.md",)
                                   if (pages_dir / x).exists()), None)
                    if target is None:
                        target = next(p.relative_to(pages_dir).as_posix() for p in sorted(pages_dir.rglob("*.md"))
                                      if p.relative_to(pages_dir).parts[0] in ("concepts", "entities"))
                    ours = [call("save_knowledge", page_id=biz["page_id"], evidence=[ev],
                                 facts=[{"text": f"校验：并发写入事实 {i}", "evidence": ["s"]}]) for i in range(5)]
                    theirs = [llm.call("wiki_update_page", {"pagePath": target, "bodyAppend": f"校验并发追加 {i}"})
                              for i in range(5)]
                    out = await asyncio.gather(*ours, *theirs, return_exceptions=True)
                    errs = [repr(x) for x in out if isinstance(x, BaseException)]
                    assert not errs, errs
                    page = await call("read_page", path=biz["page_id"])
                    have = {f["text"] for f in page["knowledge"]["facts"]}
                    assert {f"校验：并发写入事实 {i}" for i in range(5)} <= have
                    text = (pages_dir / target).read_text(encoding="utf-8")
                    assert all(f"校验并发追加 {i}" in text for i in range(5))
                    idx = await llm.call("wiki_read_index")
                    assert biz["path"] in idx["text"]
                    assert not (pages_dir / ".llmwiki.lock").exists()
                    return {"llmwiki_target": target, "gateway_saves": 5, "llmwiki_updates": 5,
                            "pages_listed": len(entries["text"])}

                async def move_keeps_references():
                    biz = state["biz"]
                    prev = await call("organize_knowledge", action="move", page=biz["page_id"],
                                      new_slug="kbw-verify-business-flow-moved")
                    assert prev["status"] == "preview"
                    done = await call("organize_knowledge", action="move", page=biz["page_id"],
                                      new_slug="kbw-verify-business-flow-moved", plan_token=prev["plan_token"])
                    assert done["status"] == "applied" and done["searchable"]
                    old = await call("read_page", path=biz["path"])
                    assert old["redirected_from"] == biz["path"] and old["page"] == done["to"]
                    fwp = await call("read_page", path=state["fw"]["page_id"])
                    assert Path(done["to"]).name in json.dumps(fwp["content"], ensure_ascii=False)
                    chk = await call("check_knowledge")
                    assert not [b for b in chk["broken_links"] if "kbw-verify" in b["target"]], chk["broken_links"]
                    lint = await llm.call("wiki_lint")
                    assert "kbw-verify-business-flow.md" not in lint["text"] or "broken" not in lint["text"].lower()
                    return {"moved_to": done["to"], "links_fixed": done["links_fixed"],
                            "check_summary": {k: len(v) for k, v in chk.items() if isinstance(v, list)}}

                async def existing_search_still_works():
                    first = next(p for p in sorted(pages_dir.rglob("*.md"))
                                 if p.relative_to(pages_dir).parts[0] in ("concepts", "entities"))
                    title = yaml.safe_load(first.read_text(encoding="utf-8").split("---")[1]).get("title")
                    res = await call("search_knowledge", query=title)
                    assert first.relative_to(pages_dir).as_posix() in [x["page"] for x in res["results"]], res["results"][:3]
                    return {"query": title, "top": res["results"][0]["page"]}

                await step("save framework page + business page linked to it (real MCP)", save_two)
                await step("search by question / service, read via LLMWiki wiki_read_page", search_and_read)
                await step("three saves of one topic -> one integrated page; repeated request -> no new page",
                           three_saves_one_page)
                await step("LLMWiki reads, indexes and lints the saved pages", llmwiki_reads_and_lints)
                await step("gateway and LLMWiki write concurrently under the shared lock", shared_lock)
                await step("move page: old path and id still resolve, links fixed", move_keeps_references)
                await step("existing knowledge search still works", existing_search_still_works)
    finally:
        await llm.close()
    raw_ok = raw_before is None or digest(wiki_root / "raw") == raw_before
    RESULTS.append({"step": "raw material directory unchanged", "status": "PASS" if raw_ok else "FAILED",
                    "ms": 0, "detail": None})
    shutil.rmtree(tmp, ignore_errors=True)
    head = (f"# Knowledge saving: real verification\n\n- date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
            f"- platform: {platform.platform()}\n- python: {sys.version.split()[0]}\n"
            f"- evidence repo: {repo.name} @ {commit[:12]}\n\n"
            "Runs on a temporary copy of the wiki. Timings are single measurements.\n\n"
            "| step | status | ms |\n|---|---|---|\n")
    body = "".join(f"| {r['step']} | {r['status']} | {r['ms']} |\n" for r in RESULTS)
    detail = "\n".join(f"## {r['step']}\n\n```json\n{json.dumps(r['detail'], ensure_ascii=False, indent=2, default=str)}\n```\n"
                       for r in RESULTS)
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "KNOWLEDGE-VERIFY-RESULTS.md").write_text(head + body + "\n" + detail, encoding="utf-8")
    failed = [r for r in RESULTS if r["status"] != "PASS"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed. Report: docs/KNOWLEDGE-VERIFY-RESULTS.md")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
