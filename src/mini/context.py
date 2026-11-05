"""The system prompt, built once per session and then frozen.

Thinking blocks are bound to the exact system prompt and tool set they were
produced under. So the prompt is assembled at session start, stored in the
session log, and replayed byte for byte on resume. Anything that changes
later goes in as an appended role:"system" message instead.
"""
import platform
import time
from pathlib import Path

from .tools import BASH, WINDOWS

BASE = """You are mini, a coding and research agent working in the user's terminal.

Work in the current directory unless told otherwise. Read files before you change them, prefer `edit` over rewriting files, use `find` to search file names and contents, and `bash` for git and tests. Check your work by running it when you can. When calls don't depend on each other, make them all in one response: read several files at once, or edit and test together. Keep replies short: say what you did and what you found, not how you went about it."""

DELEGATE = """Delegate a self-contained lookup to `task` when its intermediate output would clutter this conversation; you get back only the result. When the work is reading code closely (finding bugs, reviewing, debugging), read it yourself: a subagent's summary drops the details you need."""

MEMORY = """Memory: project knowledge lives in memory/*.md, routed by memory/INDEX.md. Read the area file when a task touches it. When you learn something durable (a decision, a fact about the project, a user preference), update the matching area file and, for a new area, its line in INDEX.md. Edits take effect in the next session; within this session, read the file. When you save something, merge it into what's there: replace what it supersedes and drop what's stale, so the files stay short."""

SUB = """You are a subagent of mini, doing one delegated task in the user's working directory. You have read, find, bash and web_search. Do not change files. Investigate, then reply with a complete, self-contained answer: the lead agent sees only your final message."""


def read_if(path: Path, cap: int = 20_000) -> str | None:
    return path.read_text(encoding="utf-8", errors="replace")[:cap] if path.is_file() else None


def build_system(cwd: str, skills: list[dict], mcp_errors: list[str], sub: bool = False, model: str = "",
                 delegate: bool = True) -> str:
    root = Path(cwd)
    shell = ("Git Bash" if BASH else "cmd.exe") if WINDOWS else "bash" if BASH else "/bin/sh, not bash"
    parts = [SUB if sub else BASE + ("\n\n" + DELEGATE if delegate else ""), f"Environment: {platform.system()} (the bash tool runs {shell}) · model {model} · cwd {root.resolve()} · session started {time.strftime('%Y-%m-%d %H:%M %Z')}"]
    if (agents := read_if(root / "AGENTS.md")):
        parts.append(f"<project_instructions file=\"AGENTS.md\">\n{agents}\n</project_instructions>")
    if sub:
        return "\n\n".join(parts)
    index = read_if(root / "memory" / "INDEX.md")  # without one, say so: models otherwise go looking for it
    parts.append(MEMORY + (f"\n<memory_index file=\"memory/INDEX.md\">\n{index}\n</memory_index>" if index else
                           "\nThere is no memory/ folder yet; create memory/INDEX.md when there is something to keep."))
    if skills:
        lines = "\n".join(f"- {s['name']}: {s['description']} ({s['path']})" for s in skills)
        parts.append(f"Skills: when a task matches one, read its SKILL.md first and follow it.\n{lines}")
    if mcp_errors:
        parts.append("MCP servers that failed to start: " + "; ".join(mcp_errors))
    return "\n\n".join(parts)
