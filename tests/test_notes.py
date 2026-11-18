"""Knowledge saving and organising: the acceptance scenarios, run against the demo
wiki and demo repositories (LLMWiki and CodeGraph downstreams disabled; the real
LLMWiki interplay is checked by scripts/verify_knowledge_save.py)."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kbw import examples
from kbw.config import load_config
from kbw.gateway import Gateway
from kbw.notes import KnowledgeError, _read_index, split_page

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def project(tmp_path):
    cfg_path = tmp_path / "kbw.yaml"
    examples.create(tmp_path, cfg_path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["downstream"] = {"llmwiki": {"enabled": False}, "codegraph": {"command": "kbw-not-installed"}}
    raw["timeouts"] = {"idle_seconds": 0}
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return tmp_path


def head(project, repo):
    return subprocess.run(["git", "-C", str(project / "examples/repos" / repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def framework_page(project, **kw):
    c = head(project, "order-service")
    return {"slug": "cloudsop-storage-usage", "kind": "framework", "title": "CloudSOP 入库通用用法",
            "summary": "通过 OrderEventPublisher 发布事件，由平台侧入库。", "services": ["order-service"],
            "keywords": ["CloudSOP", "入库", "事件发布"],
            "evidence": [{"ref": "pub", "repo": "order-service",
                          "path": "src/main/java/com/example/order/OrderEventPublisher.java",
                          "symbol": "OrderEventPublisher.publish", "lines": [3, 5], "commit": c, "branch": "master"}],
            "facts": [{"text": "入库前先调用 publish(topic, order) 发布事件", "aspect": "usage", "evidence": ["pub"]}],
            "question": "CloudSOP 入库怎么用", **kw}


def business_page(project, **kw):
    c = head(project, "order-service")
    return {"slug": "alarm-push-cloudsop-storage", "kind": "implementation",
            "title": "告警推送规则的 CloudSOP 入库流程",
            "summary": "告警推送规则变更后发布 order.cancelled 事件并由 CloudSOP 入库。",
            "services": ["order-service", "inventory-service"], "keywords": ["告警推送规则", "CloudSOP", "入库"],
            "evidence": [{"ref": "svc", "repo": "order-service",
                          "path": "src/main/java/com/example/order/OrderService.java",
                          "symbol": "OrderService.cancelOrder", "lines": [27, 35], "commit": c, "branch": "master"}],
            "facts": [{"text": "取消时先校验状态为 PENDING_PAYMENT", "aspect": "rule", "evidence": ["svc"]},
                      {"text": "随后释放库存并发布 order.cancelled", "aspect": "flow", "evidence": ["svc"]}],
            "open_questions": ["入库失败是否重试"],
            "question": "告警推送规则怎么使用 CloudSOP 入库", **kw}


def gw_of(project):
    return Gateway(load_config(str(project / "kbw.yaml")))


def raw_digest(project):
    root = project / "examples/kb/.wiki/raw"
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())).hexdigest()


def pages(project, sub=""):
    return sorted(p.relative_to(project / "examples/kb/.wiki/wiki").as_posix()
                  for p in (project / "examples/kb/.wiki/wiki" / sub).rglob("*.md"))


def test_save_search_read_and_links_over_real_mcp(project):
    """Real MCP client -> real `python -m kbw serve`: save, search, read, cross-links."""
    before_raw = raw_digest(project)

    async def go():
        env = dict(os.environ, KBW_CONFIG=str(project / "kbw.yaml"), PYTHONPATH=str(ROOT))
        params = StdioServerParameters(command=sys.executable, args=["-m", "kbw", "serve"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                async def call(tool, **args):
                    res = await s.call_tool(tool, args)
                    return json.loads(res.content[0].text)

                fw = await call("save_knowledge", **framework_page(project))
                assert fw["status"] == "created" and fw["index"]["status"] == "indexed" and fw["searchable"]
                biz = await call("save_knowledge", **business_page(project, related=[fw["page_id"]]))
                assert biz["status"] == "created" and biz["path"] == "analyses/order-service/alarm-push-cloudsop-storage.md"
                assert biz["related_updated"] == [fw["path"]]  # back-link written on the framework page

                # search by original question, by topic keyword, and restricted to a service
                for q in ("告警推送规则怎么使用 CloudSOP 入库", "告警推送规则", "PENDING_PAYMENT"):
                    res = await call("search_knowledge", query=q)
                    assert res["results"][0]["page_id"] == biz["page_id"], q
                    assert all(not x["page"].startswith("hubs/") and x["page"] != "index.md" for x in res["results"])
                inv = await call("search_knowledge", query="CloudSOP 入库", service="inventory-service")
                assert [x["page_id"] for x in inv["results"] if x["page_id"]] == [biz["page_id"]]
                assert all("inventory" in json.dumps(x, ensure_ascii=False).lower() for x in inv["results"])

                page = await call("read_page", path=biz["page_id"])
                assert page["page_id"] == biz["page_id"] and page["page"] == biz["path"]
                assert page["redirected_from"] == biz["page_id"] and page["version"] == biz["version"]
                assert page["knowledge"]["kind"] == "implementation"
                assert page["source_check"]["summary"] == "unchanged"
                assert "## 已确认的现有实现" in page["content"] and "## 待确认" in page["content"]
                assert "[CloudSOP 入库通用用法](../../frameworks/cloudsop-storage-usage.md)" in page["content"]
                fwp = await call("read_page", path=fw["path"])
                assert biz["page_id"] in fwp["knowledge"]["related"]
                assert "(../analyses/order-service/alarm-push-cloudsop-storage.md)" in fwp["content"]

                cands = await call("find_knowledge", title="告警推送 CloudSOP 入库", services=["order-service"],
                                   question="告警推送规则怎么入库")
                assert cands["candidates"][0]["page_id"] == biz["page_id"]
                st = await call("service_status")
                assert st["knowledge_writes"] == {"enabled": True, "pending_reindex": []}
    asyncio.run(go())
    assert raw_digest(project) == before_raw  # raw material untouched
    idx = {e["path"]: e for e in _read_index(project / "examples/kb/.wiki/wiki/index.md")}
    assert idx["frameworks/cloudsop-storage-usage.md"]["category"] == "Frameworks"
    assert idx["hubs/order-service.md"]["category"] == "Hubs"
    assert "entities/order-service.md" in idx  # existing entries kept
    log = (project / "examples/kb/.wiki/wiki/log.md").read_text(encoding="utf-8")
    assert "created | analyses/order-service/alarm-push-cloudsop-storage.md" in log
    hub = (project / "examples/kb/.wiki/wiki/hubs/inventory-service.md").read_text(encoding="utf-8")
    assert "告警推送规则的 CloudSOP 入库流程" in hub
    assert not list((project / "examples/kb/.wiki/wiki").rglob(".kbw-*.tmp"))


def test_three_saves_of_one_topic_make_one_integrated_page(project):
    gw = gw_of(project)
    n = gw.notes
    c = head(project, "order-service")
    try:
        r1 = n.save(business_page(project, request_id="req-1"))
        assert r1["status"] == "created"
        # second analysis: found by find_knowledge, saved into the same page
        found = n.find("告警推送规则 CloudSOP 入库", ["order-service"], ["告警推送规则"],
                       question="告警推送配置变更后怎么入库")["candidates"]
        assert found[0]["page_id"] == r1["page_id"] and found[0]["strong"]
        r2 = n.save({"page_id": found[0]["page_id"], "question": "告警推送配置变更后怎么入库",
                     "summary": "", "keywords": ["告警推送规则", "入库重试"],
                     "evidence": [{"ref": "a", "repo": "inventory-service",
                                   "path": "src/main/java/com/example/inventory/OrderCancelledListener.java",
                                   "symbol": "OrderCancelledListener.onMessage", "lines": [12, 14], "commit": head(project, "inventory-service")},
                                  {"ref": "b", "repo": "order-service",
                                   "path": "src/main/java/com/example/order/OrderService.java",
                                   "symbol": "OrderService.cancelOrder", "lines": [27, 35], "commit": c}],
                     "facts": [{"text": "库存服务消费 order.cancelled 做兜底释放", "aspect": "flow", "evidence": ["a"]},
                               {"text": "取消时先校验状态为 PENDING_PAYMENT。", "aspect": "rule", "evidence": ["b"]}],
                     "inferences": ["入库失败可能依赖消息重投"], "resolve": ["入库失败是否重试"],
                     "request_id": "req-2"})
        assert r2["status"] == "updated" and r2["path"] == r1["path"]
        assert r2["changes"]["added"] == {"keywords": 1, "evidence": 1, "facts": 1, "inferences": 1, "questions": 1}
        assert r2["changes"]["duplicates_skipped"] >= 2   # same fact (punctuation differs), same evidence
        # third save of the same topic by slug: lands on the same page
        r3 = n.save(business_page(project, question="CloudSOP 入库时告警推送规则校验什么", request_id="req-3",
                                  facts=[{"text": "取消时先校验状态为 PENDING_PAYMENT", "aspect": "rule",
                                          "evidence": ["svc"]}]))
        assert r3["status"] == "updated" and r3["path"] == r1["path"]
        assert any("updating it instead of creating" in w for w in r3["warnings"])
        # the same request again: no new page, nothing written
        again = n.save(business_page(project, request_id="req-3"))
        assert again["status"] == "duplicate_request" and again["page_id"] == r1["page_id"]
        # identical content without request id: unchanged, not rewritten
        v = r3["version"]
        same = n.save(business_page(project, question="CloudSOP 入库时告警推送规则校验什么"))
        assert same["status"] == "unchanged" and same["version"] == v

        assert pages(project, "analyses") == ["analyses/order-service/alarm-push-cloudsop-storage.md"]
        page = asyncio.run(gw.read_page(r1["page_id"]))
        k = page["knowledge"]
        assert [f["text"] for f in k["facts"]] == ["取消时先校验状态为 PENDING_PAYMENT", "随后释放库存并发布 order.cancelled",
                                                   "库存服务消费 order.cancelled 做兜底释放"]
        assert len(k["evidence"]) == 2 and k["open_questions"] == []
        assert len(k["questions"]) == 3
        assert "入库失败是否重试（已解答" in page["content"]
        for q in ("告警推送配置变更后怎么入库", "CloudSOP 入库时告警推送规则校验什么", "兜底释放"):
            res = asyncio.run(gw.search_knowledge(q))
            assert res["results"][0]["page_id"] == r1["page_id"], q
    finally:
        asyncio.run(gw.close())


def test_sub_topic_needs_decision_then_links(project):
    gw = gw_of(project)
    try:
        a = gw.notes.save(business_page(project))
        near = business_page(project, slug="alarm-push-cloudsop-retry", title="告警推送规则 CloudSOP 入库重试")
        r = gw.notes.save(near)
        assert r["status"] == "needs_decision" and r["candidates"][0]["page_id"] == a["page_id"]
        assert pages(project, "analyses") == ["analyses/order-service/alarm-push-cloudsop-storage.md"]
        r = gw.notes.save({**near, "distinct_from": [a["page_id"]]})
        assert r["status"] == "created" and r["related_updated"] == [a["path"]]
        pa = asyncio.run(gw.read_page(a["page_id"]))
        assert r["page_id"] in pa["knowledge"]["related"]
    finally:
        asyncio.run(gw.close())


def test_no_silent_overwrite_and_concurrent_merges(project):
    gw = gw_of(project)
    n = gw.notes
    c = head(project, "order-service")
    ev = [{"ref": "x", "repo": "order-service", "path": "src/main/java/com/example/order/Order.java", "commit": c}]
    try:
        a = n.save(business_page(project))
        stale = a["version"]
        n.save({"page_id": a["page_id"], "facts": [{"text": "订单有三个状态", "evidence": ["x"]}], "evidence": ev})
        with pytest.raises(KnowledgeError, match="expected_version"):
            n.save({"page_id": a["page_id"], "mode": "replace", "summary": "x"})
        r = n.save({**business_page(project), "page_id": a["page_id"], "mode": "replace", "expected_version": stale})
        assert r["status"] == "conflict" and r["current_version"] != stale

        # 8 writers at once, each adding a different fact: nothing lost, one page
        errors = []

        def add(i):
            try:
                n.save({"page_id": a["page_id"], "evidence": ev,
                        "facts": [{"text": f"并发写入的事实 {i}", "evidence": ["x"]}]})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        k = asyncio.run(gw.read_page(a["page_id"]))["knowledge"]
        assert {f"并发写入的事实 {i}" for i in range(8)} <= {f["text"] for f in k["facts"]}

        # a curated rewrite drops nothing silently: left-out items move to history
        cur = asyncio.run(gw.read_page(a["page_id"]))
        r = n.save({**business_page(project), "page_id": a["page_id"], "mode": "replace",
                    "expected_version": cur["version"], "title": "告警推送规则的 CloudSOP 入库流程"})
        assert r["status"] == "updated" and r["changes"]["moved_to_history"] >= 9
        body = asyncio.run(gw.read_page(a["page_id"]))["content"]
        assert "并发写入的事实 3（整理时移除" in body

        # a body edited by hand is never overwritten
        f = project / "examples/kb/.wiki/wiki" / a["path"]
        f.write_text(f.read_text(encoding="utf-8").replace("## 概述", "## 概述\n\n手工补充的一段话"), encoding="utf-8")
        r = n.save({"page_id": a["page_id"], "keywords": ["新词"]})
        assert r["status"] == "conflict" and "edited outside" in r["message"]
        assert "手工补充的一段话" in f.read_text(encoding="utf-8")
    finally:
        asyncio.run(gw.close())


def test_llmwiki_lock_is_respected(project):
    gw = gw_of(project)
    lock = project / "examples/kb/.wiki/wiki/.llmwiki.lock"
    try:
        # a live lock (as held by an LLMWiki write): the save waits for it
        lock.write_text("")
        stop = threading.Event()

        def holder():
            end = time.time() + 1.2
            while time.time() < end:
                os.utime(lock)
                time.sleep(0.2)
            lock.unlink()
        t = threading.Thread(target=holder)
        t0 = time.time()
        t.start()
        r = gw.notes.save(framework_page(project))
        t.join()
        assert r["status"] == "created" and time.time() - t0 >= 1.0
        # timeout while another writer holds it: nothing written
        lock.write_text("")
        gw.notes.lock_timeout = 0.4
        keep = threading.Thread(target=lambda: [os.utime(lock) or time.sleep(0.1) for _ in range(10)])
        keep.start()
        with pytest.raises(TimeoutError):
            gw.notes.save(business_page(project))
        keep.join()
        assert pages(project, "analyses") == []
        # a stale lock (no heartbeat for > 10 s) is taken over
        old = time.time() - 60
        os.utime(lock, (old, old))
        assert gw.notes.save(business_page(project))["status"] == "created"
        assert not lock.exists()
    finally:
        asyncio.run(gw.close())


def test_index_failure_keeps_page_and_retry_recovers(project, monkeypatch):
    gw = gw_of(project)
    try:
        real = gw.store.add_page
        monkeypatch.setattr(gw.store, "add_page", lambda *a, **k: (_ for _ in ()).throw(OSError("disk busy")))
        r = gw.notes.save(business_page(project))
        assert r["status"] == "created" and r["searchable"] is False
        assert r["index"]["status"] == "failed" and "disk busy" in r["index"]["error"]
        assert (project / "examples/kb/.wiki/wiki" / r["path"]).is_file()
        assert gw.notes.pending()[0]["path"] == r["path"]
        assert asyncio.run(gw.status())["knowledge_writes"]["pending_reindex"]
        monkeypatch.setattr(gw.store, "add_page", real)
        again = gw.notes.reindex()
        assert again["status"] == "indexed" and r["path"] in again["recovered"]
        assert gw.notes.pending() == []
        res = asyncio.run(gw.search_knowledge("告警推送规则"))
        assert res["results"][0]["page_id"] == r["page_id"]
    finally:
        asyncio.run(gw.close())


@pytest.mark.parametrize("change,msg", [
    ({"facts": [{"text": "配置 password=Abc12345xyz 用于入库", "evidence": ["svc"]}]}, "credential"),
    ({"facts": [{"text": "Authorization: Bearer abcdefghijklmnop1234567890", "evidence": ["svc"]}]}, "credential"),
    ({"evidence": [{"ref": "svc", "repo": "order-service", "path": "../secret.txt"}]}, "relative path"),
    ({"evidence": [{"ref": "svc", "repo": "order-service", "path": "C:/x/OrderService.java"}]}, "relative path"),
    ({"evidence": [{"ref": "svc", "repo": "order-service", "path": "a.java", "lines": [9, 3]}]}, "lines"),
    ({"evidence": [{"ref": "svc", "repo": "order-service", "path": "a.java", "commit": "zzzz"}]}, "commit"),
    ({"facts": [{"text": "没有依据的结论"}]}, "no evidence"),
    ({"slug": "告警推送"}, "slug"),
    ({"kind": "guess"}, "kind"),
    ({"services": ["../x"]}, "service name"),
])
def test_validation_rejects_without_writing(project, change, msg):
    gw = gw_of(project)
    try:
        with pytest.raises(KnowledgeError, match=msg):
            gw.notes.save(business_page(project, **change))
        assert pages(project, "analyses") == []
    finally:
        asyncio.run(gw.close())


def test_move_and_merge_keep_old_references(project):
    gw = gw_of(project)
    n = gw.notes
    try:
        fw = n.save(framework_page(project))
        biz = n.save(business_page(project, related=[fw["page_id"]]))
        old = biz["path"]
        prev = n.organize("move", biz["page_id"], new_slug="alarm-rule-cloudsop-storage")
        assert prev["status"] == "preview" and prev["plan"]["pages_with_links_to_fix"]
        assert (project / "examples/kb/.wiki/wiki" / old).exists()  # preview writes nothing
        done = n.organize("move", biz["page_id"], new_slug="alarm-rule-cloudsop-storage",
                          plan_token=prev["plan_token"])
        new = done["to"]
        assert done["status"] == "applied" and new == "analyses/order-service/alarm-rule-cloudsop-storage.md"
        by_old = asyncio.run(gw.read_page(old))
        assert by_old["redirected_from"] == old and by_old["page_id"] == biz["page_id"]
        assert asyncio.run(gw.read_page(biz["page_id"]))["page_id"] == biz["page_id"]
        fw_body = asyncio.run(gw.read_page(fw["page_id"]))["content"]
        assert "../analyses/order-service/alarm-rule-cloudsop-storage.md" in fw_body and old not in fw_body
        assert new in {e["path"] for e in _read_index(project / "examples/kb/.wiki/wiki/index.md")}
        assert old not in {e["path"] for e in _read_index(project / "examples/kb/.wiki/wiki/index.md")}
        assert asyncio.run(gw.search_knowledge("告警推送规则"))["results"][0]["page"] == new
        assert n.check()["broken_links"] == []

        # merge a second page about the same topic into the first
        dup = n.save(business_page(project, slug="alarm-cloudsop-storage-dup", title="告警推送 CloudSOP 入库说明",
                                   distinct_from=[biz["page_id"]], question="告警规则入库时序",
                                   facts=[{"text": "取消时先校验状态为 PENDING_PAYMENT", "aspect": "rule", "evidence": ["svc"]},
                                          {"text": "取消时先校验状态是否为 PENDING_PAYMENT 并记录日志", "aspect": "rule",
                                           "evidence": ["svc"]}]))
        assert dup["status"] == "created"
        prev = n.organize("merge", dup["page_id"], target=biz["page_id"])
        diff = prev["plan"]["diff"]
        assert diff["duplicates_collapsed"] >= 1 and diff["possible_conflicts"]
        # the target changes after the preview: the stale plan is refused
        n.save({"page_id": biz["page_id"], "keywords": ["变更"]})
        stale = n.organize("merge", dup["page_id"], target=biz["page_id"], plan_token=prev["plan_token"])
        assert stale["status"] == "conflict"
        prev = n.organize("merge", dup["page_id"], target=biz["page_id"])
        done = n.organize("merge", dup["page_id"], target=biz["page_id"], plan_token=prev["plan_token"])
        assert done["status"] == "applied"
        assert not (project / "examples/kb/.wiki/wiki" / dup["path"]).exists()
        via_old_id = asyncio.run(gw.read_page(dup["page_id"]))
        assert via_old_id["page_id"] == biz["page_id"]
        assert asyncio.run(gw.read_page(dup["path"]))["page_id"] == biz["page_id"]
        k = via_old_id["knowledge"]
        assert "告警规则入库时序" in k["questions"]
        assert sum(f["text"].startswith("取消时先校验状态") for f in k["facts"]) == 2
        assert asyncio.run(gw.search_knowledge("告警规则入库时序"))["results"][0]["page_id"] == biz["page_id"]
        assert pages(project, "analyses") == [new]
    finally:
        asyncio.run(gw.close())


def test_check_knowledge_reports_and_fixes(project):
    gw = gw_of(project)
    n = gw.notes
    wiki = project / "examples/kb/.wiki/wiki"
    try:
        biz = n.save(business_page(project))
        n.save(business_page(project, slug="alarm-push-cloudsop-copy", title="告警推送规则 CloudSOP 入库",
                             distinct_from=[biz["page_id"]]))
        prev = n.organize("move", biz["page_id"], new_slug="alarm-push-storage")
        n.organize("move", biz["page_id"], new_slug="alarm-push-storage", plan_token=prev["plan_token"])
        # a hand-written page still links to the old path
        concept = wiki / "concepts/order-timeout-cancel.md"
        concept.write_text(concept.read_text(encoding="utf-8")
                           + "\n见 [入库流程](../analyses/order-service/alarm-push-cloudsop-storage.md)\n", encoding="utf-8")
        # the cited source changes in a new commit
        repo = project / "examples/repos/order-service"
        f = repo / "src/main/java/com/example/order/OrderService.java"
        f.write_text(f.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-qam", "change"], check=True)

        rep = n.check()
        assert rep["duplicates"] and {rep["duplicates"][0]["a"], rep["duplicates"][0]["b"]} >= {
            "analyses/order-service/alarm-push-storage.md"}
        b = next(x for x in rep["broken_links"] if x["page"] == "concepts/order-timeout-cancel.md")
        assert b["redirect_to"] == "analyses/order-service/alarm-push-storage.md"
        assert any(s["page"] == "analyses/order-service/alarm-push-storage.md" for s in rep["possibly_stale"])
        assert any(m["page"] == "entities/inventory-service.md" for m in rep["missing_sources"])
        assert "concepts/stock-leak-troubleshooting.md" in rep["isolated"]       # nothing links to it
        assert not any(p.startswith(("hubs/", "sources/")) for p in rep["isolated"])
        src = asyncio.run(gw.read_page(biz["page_id"]))["source_check"]
        assert src["summary"] == "needs_review" and src["evidence"][0]["status"] == "changed_since_recorded"

        fixed = n.check(fix=True)
        assert "concepts/order-timeout-cancel.md" in fixed["fixed"]["links"]
        assert "../analyses/order-service/alarm-push-storage.md" in concept.read_text(encoding="utf-8")
        assert n.check()["broken_links"] == []
    finally:
        asyncio.run(gw.close())


def test_pages_stay_llmwiki_compatible(project):
    gw = gw_of(project)
    try:
        r = gw.notes.save(business_page(project))
        fm, body = split_page((project / "examples/kb/.wiki/wiki" / r["path"]).read_text(encoding="utf-8"))
        assert fm["type"] == "concept" and fm["title"] and fm["created"] and fm["updated"]
        assert all(isinstance(t, str) for t in fm["tags"]) and "implementation" in fm["tags"]
        assert isinstance(fm["created"], str)  # quoted: YAML readers keep it as text
        assert body.startswith("\n# 告警推送规则的 CloudSOP 入库流程") or body.lstrip().startswith("# ")
        reg = json.loads((project / "examples/kb/.wiki/wiki/.kbw/registry.json").read_text(encoding="utf-8"))
        assert reg["ids"][r["page_id"]] == r["path"]
        gw.cfg.knowledge_writable = False
        with pytest.raises(PermissionError):
            gw.notes.save(framework_page(project))
    finally:
        asyncio.run(gw.close())
