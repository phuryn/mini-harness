"""Four local tools (read, write, edit, bash) plus the approval gate.

A tool is a JSON schema the model sees and a Python function the harness runs.
`edit` refuses to touch a file that changed since the model last read it.
"""
import os
import subprocess
from pathlib import Path

MAX_OUT = 30_000  # characters returned to the model per tool call


class ToolError(Exception):
    pass


def truncate(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    half = MAX_OUT // 2
    return f"{text[:half]}\n\n[... {len(text) - MAX_OUT} characters cut ...]\n\n{text[-half:]}"


class Approver:
    """Asks before risky calls. `auto=True` approves everything (headless --yes)."""

    SAFE = ("ls", "cat ", "head ", "tail ", "wc ", "grep ", "rg ", "find ", "pwd", "git status",
            "git diff", "git log", "git show", "echo ")

    def __init__(self, auto: bool = False, interactive: bool = True, allow: tuple = ()):
        self.auto, self.interactive, self.always, self.allow = auto, interactive, set(), tuple(allow)

    def ok(self, kind: str, detail: str) -> bool:
        if self.auto or kind in self.always:
            return True
        if kind == "bash" and detail.startswith(self.SAFE + self.allow) and not any(c in detail for c in ";&|><`$"):
            return True
        if not self.interactive:
            return False
        ans = input(f"\033[33m  allow {kind}: {detail[:200]}  [y/n/a=always] \033[0m").strip().lower()
        if ans == "a":
            self.always.add(kind)
        return ans in ("y", "a", "yes")


class LocalTools:
    def __init__(self, cwd: str, approver: Approver):
        self.cwd = Path(cwd).resolve()
        self.approver = approver
        self.seen: dict[Path, float] = {}  # path -> mtime when the model last read or wrote it

    def _path(self, p: str) -> Path:
        path = Path(p).expanduser()
        return (path if path.is_absolute() else self.cwd / path).resolve()

    def _inside(self, path: Path) -> bool:
        return path == self.cwd or self.cwd in path.parents

    def read(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        p = self._path(path)
        if p.is_dir():
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))
        lines = p.read_text(errors="replace").splitlines()
        self.seen[p] = p.stat().st_mtime
        start = max(offset, 1)
        chunk = lines[start - 1:start - 1 + limit]
        body = "\n".join(f"{i:>5}\t{line}" for i, line in enumerate(chunk, start))
        more = len(lines) - (start - 1 + len(chunk))
        return body + (f"\n[{more} more lines; use offset]" if more > 0 else "")

    def write(self, path: str, content: str) -> str:
        p = self._path(path)
        if p.exists() and p not in self.seen:
            raise ToolError("File exists. Read it first, or use edit.")
        if not self._inside(p) and not self.approver.ok("write", str(p)):
            raise ToolError("User denied writing outside the working directory.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.seen[p] = p.stat().st_mtime
        return f"Wrote {len(content.splitlines())} lines to {p}"

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = self._path(path)
        if p not in self.seen:
            raise ToolError("Read the file before editing it.")
        if p.stat().st_mtime != self.seen[p]:
            raise ToolError("File changed since you last read it. Read it again.")
        if not self._inside(p) and not self.approver.ok("write", str(p)):
            raise ToolError("User denied editing outside the working directory.")
        text = p.read_text()
        n = text.count(old)
        if n == 0:
            raise ToolError("`old` not found. It must match the file exactly, including whitespace.")
        if n > 1 and not replace_all:
            raise ToolError(f"`old` appears {n} times. Add context to make it unique, or set replace_all.")
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        self.seen[p] = p.stat().st_mtime
        return f"Edited {p} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''})"

    def bash(self, command: str, timeout: int = 120) -> str:
        if not self.approver.ok("bash", command):
            raise ToolError("User denied this command.")
        try:
            r = subprocess.run(command, shell=True, cwd=self.cwd, capture_output=True, text=True,
                               timeout=timeout, env={**os.environ, "PAGER": "cat"})
        except subprocess.TimeoutExpired:
            raise ToolError(f"Timed out after {timeout}s.")
        out = (r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr else "")).strip()
        return f"{out}\n[exit {r.returncode}]" if r.returncode else (out or "[no output]")


SCHEMAS = {
    "read": {
        "description": "Read a text file (numbered lines) or list a directory.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "First line, 1-based"},
            "limit": {"type": "integer"}}, "required": ["path"]}},
    "write": {
        "description": "Create a file, or overwrite one you have read.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    "edit": {
        "description": "Replace an exact string in a file you have read. `old` must be unique unless replace_all.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
            "replace_all": {"type": "boolean"}}, "required": ["path", "old", "new"]}},
    "bash": {
        "description": "Run a shell command in the working directory. Use it for search (grep, find), git and tests.",
        "input_schema": {"type": "object", "properties": {
            "command": {"type": "string"}, "timeout": {"type": "integer", "description": "Seconds, default 120"}},
            "required": ["command"]}},
}

WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search"}
