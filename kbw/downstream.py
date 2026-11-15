"""Managed long-lived MCP client sessions to downstream servers (official SDK).

One ManagedSession per downstream program:
- lazy start on first call, single-flight (concurrent first calls start ONE process)
- long-lived reuse, per-call timeout
- reconnect after the downstream crashed or timed out
- idle release
- on close only our own child process is terminated (the SDK's stdio transport
  owns it). Anything the downstream itself spawned and detached, such as
  CodeGraph's shared per-project daemon, is left to its own lifecycle.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .util import log


class DownstreamUnavailable(RuntimeError):
    """The downstream cannot be started or is not configured."""


class DownstreamToolError(RuntimeError):
    """The downstream answered, but reported a tool-level error."""


@dataclass
class Stats:
    starts: int = 0
    calls: int = 0
    failures: int = 0
    last_error: str | None = None
    last_start_ms: float | None = None
    started_at: float | None = None
    tools: list[str] = field(default_factory=list)


class ManagedSession:
    def __init__(self, name: str, command: str | None, args: list[str], *, env: dict | None = None,
                 cwd: str | None = None, call_timeout: float = 30, start_timeout: float = 30,
                 idle_timeout: float = 300, required_tools: list[str] | None = None,
                 allowed_tools: list[str] | None = None, unavailable_reason: str | None = None):
        self.name = name
        self._command, self._args, self._env, self._cwd = command, args, env, cwd
        self.call_timeout, self.start_timeout, self.idle_timeout = call_timeout, start_timeout, idle_timeout
        self.required_tools = required_tools or []
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        self.unavailable_reason = unavailable_reason
        self.stats = Stats()
        self._session: ClientSession | None = None
        self._runner: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._last_used = 0.0
        self._idle_task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def state(self) -> str:
        if self.unavailable_reason:
            return "unavailable"
        return "connected" if self._session is not None else "idle"

    async def _run(self, ready: asyncio.Future) -> None:
        """Owns the transport and session for their whole life. They are entered
        and exited in this one task, which the SDK's cancel scopes require."""
        params = StdioServerParameters(command=self._command, args=self._args, env=self._env, cwd=self._cwd)
        try:
            async with stdio_client(params, errlog=sys.stderr) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = [t.name for t in listed.tools]
                    missing = [t for t in self.required_tools if t not in names]
                    if missing:
                        raise DownstreamUnavailable(
                            f"{self.name}: required tools missing: {missing}; server offers {names}")
                    self.stats.tools = names
                    self._session = session
                    if not ready.done():
                        ready.set_result(None)
                    await self._stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(DownstreamUnavailable(f"{self.name}: start cancelled"))
            raise
        except BaseException as exc:  # includes exception groups from the SDK's task group
            self.stats.last_error = _describe(exc)
            if not ready.done():
                ready.set_exception(DownstreamUnavailable(f"{self.name}: {self.stats.last_error}"))
            else:
                log.warning("downstream %s connection ended: %s", self.name, self.stats.last_error)
        finally:
            self._session = None

    async def _ensure(self) -> ClientSession:
        if self.unavailable_reason:
            raise DownstreamUnavailable(f"{self.name}: {self.unavailable_reason}")
        async with self._lock:  # single-flight
            if self._session is not None and self._runner and not self._runner.done():
                return self._session
            await self._teardown()
            t0 = time.perf_counter()
            self._stop = asyncio.Event()
            ready = asyncio.get_running_loop().create_future()
            self._runner = asyncio.create_task(self._run(ready), name=f"kbw-{self.name}")
            try:
                await asyncio.wait_for(asyncio.shield(ready), self.start_timeout)
            except asyncio.TimeoutError:
                await self._teardown()
                self.stats.last_error = f"start timed out after {self.start_timeout}s"
                raise DownstreamUnavailable(f"{self.name}: {self.stats.last_error}") from None
            except DownstreamUnavailable:
                await self._teardown()
                raise
            self.stats.starts += 1
            self.stats.last_start_ms = round((time.perf_counter() - t0) * 1000, 1)
            self.stats.started_at = time.time()
            self._last_used = time.monotonic()
            log.info("downstream %s started in %sms, tools=%s", self.name, self.stats.last_start_ms,
                     self.stats.tools)
            if self.idle_timeout > 0 and (self._idle_task is None or self._idle_task.done()):
                self._idle_task = asyncio.create_task(self._idle_watch(), name=f"kbw-{self.name}-idle")
            return self._session

    async def _teardown(self) -> None:
        runner, stop = self._runner, self._stop
        self._runner, self._session = None, None
        if runner is None:
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(runner), 10)
        except asyncio.TimeoutError:
            runner.cancel()
            try:
                await runner
            except BaseException:
                pass
        except BaseException:
            pass

    async def _idle_watch(self) -> None:
        while True:
            await asyncio.sleep(max(0.05, min(self.idle_timeout / 2, 30)))
            if self._session is None:
                return
            if self._inflight == 0 and time.monotonic() - self._last_used >= self.idle_timeout:
                async with self._lock:
                    if self._inflight == 0 and self._session is not None:
                        log.info("downstream %s idle for %ss, releasing", self.name, self.idle_timeout)
                        await self._teardown()
                return

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()

    # ------------------------------------------------------------ calls

    async def call(self, tool: str, arguments: dict | None = None, timeout: float | None = None) -> dict:
        """Call a downstream tool. Returns {"text": str, "json": parsed-or-None,
        "structured": dict-or-None}. One transparent retry after a dead connection."""
        if self.allowed_tools is not None and tool not in self.allowed_tools:
            raise PermissionError(f"{self.name}: tool '{tool}' is not on the gateway allow-list")
        timeout = timeout or self.call_timeout
        last: BaseException | None = None
        for attempt in (1, 2):
            session = await self._ensure()
            self._inflight += 1
            self.stats.calls += 1
            try:
                result = await asyncio.wait_for(session.call_tool(tool, arguments or {}), timeout)
            except asyncio.TimeoutError:
                self.stats.failures += 1
                self.stats.last_error = f"{tool} timed out after {timeout}s"
                async with self._lock:
                    await self._teardown()  # state unknown: drop the connection
                raise TimeoutError(f"{self.name}: {self.stats.last_error}") from None
            except Exception as exc:
                self.stats.failures += 1
                self.stats.last_error = _describe(exc)
                last = exc
                async with self._lock:
                    if self._session is session or self._session is None:
                        await self._teardown()
                if attempt == 1:
                    log.warning("downstream %s call failed (%s), reconnecting once", self.name,
                                self.stats.last_error)
                    continue
                raise DownstreamUnavailable(f"{self.name}: {self.stats.last_error}") from exc
            finally:
                self._inflight -= 1
                self._last_used = time.monotonic()
            text = "\n".join(getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", None))
            if getattr(result, "is_error", False):
                raise DownstreamToolError(f"{self.name}.{tool}: {text[:500]}")
            parsed = None
            try:
                parsed = json.loads(text) if text[:1] in "[{" else None
            except ValueError:
                pass
            return {"text": text, "json": parsed, "structured": getattr(result, "structured_content", None)}
        raise DownstreamUnavailable(f"{self.name}: {_describe(last)}")

    def describe(self) -> dict:
        s = self.stats
        return {"state": self.state, "reason": self.unavailable_reason, "starts": s.starts, "calls": s.calls,
                "failures": s.failures, "last_error": s.last_error, "last_start_ms": s.last_start_ms,
                "tools": s.tools, "command": [self._command, *self._args] if self._command else None}


def _describe(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown error"
    subs = getattr(exc, "exceptions", None)
    if subs:
        return "; ".join(_describe(e) for e in subs)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
