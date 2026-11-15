import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from kbw.downstream import DownstreamUnavailable, ManagedSession

FAKE = str(Path(__file__).with_name("fake_downstream.py"))


def make(tmp_path, **kw):
    pid_log = tmp_path / "pids.txt"
    env = dict(os.environ, FAKE_PID_LOG=str(pid_log))
    opts = dict(call_timeout=10, start_timeout=20, idle_timeout=0, required_tools=["echo"])
    opts.update(kw)
    return ManagedSession("fake", sys.executable, [FAKE], env=env, **opts), pid_log


def pids(pid_log):
    return [int(x) for x in pid_log.read_text().split()] if pid_log.exists() else []


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # a zombie still answers kill(0) on Linux
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def wait_dead(pid, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def run(coro):
    return asyncio.run(coro)


def test_lazy_and_single_flight(tmp_path):
    async def go():
        s, pid_log = make(tmp_path)
        assert pids(pid_log) == [] and s.state == "idle"  # nothing started before first call
        results = await asyncio.gather(*[s.call("echo", {"text": str(i)}) for i in range(12)])
        assert len(pids(pid_log)) == 1 and s.stats.starts == 1
        assert {r["text"].split(":")[0] for r in results} == {str(pids(pid_log)[0])}
        await s.call("echo", {"text": "again"})
        assert s.stats.starts == 1  # reused
        pid = pids(pid_log)[0]
        await s.close()
        assert wait_dead(pid)  # clean shutdown leaves no child
    run(go())


def test_crash_recovery(tmp_path):
    async def go():
        s, pid_log = make(tmp_path)
        await s.call("echo", {"text": "a"})
        first = pids(pid_log)[0]
        with pytest.raises(Exception):
            await s.call("crash", {})  # dies mid-call; the retry crashes again
        r = await s.call("echo", {"text": "b"})  # next call transparently reconnects
        assert r["text"].endswith(":b")
        assert int(r["text"].split(":")[0]) != first and wait_dead(first)
        await s.close()
    run(go())


def test_killed_externally_then_recovers(tmp_path):
    async def go():
        s, pid_log = make(tmp_path)
        await s.call("echo", {"text": "a"})
        first = pids(pid_log)[0]
        os.kill(first, 9)
        assert wait_dead(first)
        r = await s.call("echo", {"text": "b"})
        assert r["text"].endswith(":b") and len(pids(pid_log)) == 2
        await s.close()
    run(go())


def test_timeout_then_recovers(tmp_path):
    async def go():
        s, pid_log = make(tmp_path, call_timeout=0.5)
        with pytest.raises(TimeoutError):
            await s.call("slow", {"seconds": 5})
        r = await s.call("echo", {"text": "ok"})
        assert r["text"].endswith(":ok") and len(pids(pid_log)) == 2
        await s.close()
    run(go())


def test_idle_release(tmp_path):
    async def go():
        s, pid_log = make(tmp_path, idle_timeout=0.4)
        await s.call("echo", {"text": "a"})
        pid = pids(pid_log)[0]
        await asyncio.sleep(1.5)
        assert s.state == "idle" and wait_dead(pid)
        await s.call("echo", {"text": "b"})  # lazily restarted
        assert len(pids(pid_log)) == 2
        await s.close()
    run(go())


def test_allow_list_blocks_write_tools(tmp_path):
    async def go():
        s, pid_log = make(tmp_path, allowed_tools=["echo"])
        with pytest.raises(PermissionError):
            await s.call("write_thing", {})
        assert pids(pid_log) == []  # refused before any process is started
        await s.close()
    run(go())


def test_missing_program_and_missing_tool(tmp_path):
    async def go():
        s = ManagedSession("nope", "kbw-definitely-not-installed", [], start_timeout=5)
        with pytest.raises(DownstreamUnavailable):
            await s.call("echo", {})
        s2, _ = make(tmp_path, required_tools=["echo", "not_a_tool"])
        with pytest.raises(DownstreamUnavailable, match="required tools missing"):
            await s2.call("echo", {"text": "x"})
        await s.close(); await s2.close()
    run(go())
