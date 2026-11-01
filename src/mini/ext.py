"""Extensions: skills (a list in the system prompt) and MCP (stdio servers).

Skills need no tool: the model sees each skill's name and description, and
reads the SKILL.md with `read` when a task calls for it.

MCP servers are async; the harness is not. A background event loop owns the
connections, and calls cross over with run_coroutine_threadsafe.
"""
import asyncio
import concurrent.futures
import json
import re
import threading
from pathlib import Path


def find_skills(cwd: str) -> list[dict]:
    roots = [Path(cwd) / "skills", Path(cwd) / ".agents" / "skills", Path.home() / ".mini" / "skills"]
    skills = []
    for root in roots:
        for f in sorted(root.glob("*/SKILL.md")):
            head = f.read_text(errors="replace")[:2000]
            m = re.match(r"---\n(.*?)\n---", head, re.S)
            fields = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M)) if m else {}
            skills.append({"name": fields.get("name", f.parent.name),
                           "description": fields.get("description", "").strip("\"'"), "path": str(f)})
    return skills


def safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


class MCPHub:
    """Connects to every server in mcp.json and exposes tools as mcp__server__tool."""

    def __init__(self, cwd: str):
        self.tools: dict[str, tuple] = {}  # api name -> (session, real name)
        self.schemas: list[dict] = []
        self.errors: list[str] = []
        cfg = next((p for p in [Path(cwd) / "mcp.json", Path.home() / ".mini" / "mcp.json"] if p.exists()), None)
        if not cfg:
            return
        self.servers = json.loads(cfg.read_text()).get("mcpServers", {})
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.stop = self._run(self._make_event())
        self.tasks = []
        for name, spec in self.servers.items():
            ready = concurrent.futures.Future()
            task = asyncio.run_coroutine_threadsafe(self._serve(name, spec, ready), self.loop)
            self.tasks.append(task)
            try:
                ready.result(30)
            except Exception as e:  # one broken server must not stop the harness
                ready.cancel()
                task.cancel()
                self.errors.append(f"{name}: {e!r}"[:200])

    def _run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _make_event(self):
        return asyncio.Event()

    async def _serve(self, name, spec, ready):
        """Holds one server's connection open in a single task until close()."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=spec.get("env"))
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                listed = (await session.list_tools()).tools
                if ready.done():  # startup already timed out; don't register late tools
                    return
                for t in listed:
                    api = f"mcp__{safe_name(name)}__{safe_name(t.name)}"[:128]
                    self.tools[api] = (session, t.name)
                    self.schemas.append({"name": api, "description": (t.description or "")[:1024],
                                         "input_schema": t.input_schema or {"type": "object"}})
                ready.set_result(True)
                await self.stop.wait()
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(repr(e)))

    def call(self, api_name: str, args: dict) -> tuple[str, bool]:
        session, real = self.tools[api_name]
        fut = asyncio.run_coroutine_threadsafe(session.call_tool(real, args), self.loop)
        try:
            r = fut.result(300)
        except BaseException:  # timeout or Ctrl-C: don't leave the call running
            fut.cancel()
            raise
        parts = [getattr(c, "text", None) or f"[{c.type} content]" for c in getattr(r, "content", [])]
        return "\n".join(parts) or "[no output]", bool(getattr(r, "is_error", False))

    def close(self):
        if hasattr(self, "loop"):
            self.loop.call_soon_threadsafe(self.stop.set)
            for t in self.tasks:
                try:
                    t.result(5)
                except Exception:
                    pass
