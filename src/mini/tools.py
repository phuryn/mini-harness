"""Five local tools (read, write, edit, bash, find) plus the approval gate.

A tool is a JSON schema the model sees and a Python function the harness runs.
`edit` refuses to touch a file that changed since the model last read it.
"""
import contextlib
import fnmatch
import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

MAX_OUT = 30_000  # characters returned to the model per tool call


class ToolError(Exception):
    pass


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # terminal control codes; keeps \n, \t and readable Unicode


def printable(text: str, lines: bool = False) -> str:
    """Nothing can hide in a prompt: padding is counted, everything outside plain ASCII is escaped, and real line
    breaks are kept only where asked (file content)."""
    text = re.sub(" {16,}" if lines else " {3,}", lambda m: f"[{len(m[0])} spaces]", text)  # code keeps its indent
    text = re.sub(r"(\n[ \t]*){3,}", lambda m: f"\n[{m[0].count(chr(10)) - 1} blank lines]\n", text) if lines else text
    return "".join(c if c.isascii() and c.isprintable() and c != "\\" or lines and c == "\n"
                   else c.encode("unicode_escape").decode() for c in text)


def run_shell(command: str, cwd, timeout: int):
    """Run a shell command with no stdin; on timeout or Ctrl-C kill its whole process group."""
    proc = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PAGER": "cat"}, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):  # already gone
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    return proc.returncode, out, err


def truncate(text: str) -> str:
    h = MAX_OUT // 2
    return text if len(text) <= MAX_OUT else f"{text[:h]}\n\n[... {len(text) - MAX_OUT} characters cut ...]\n\n{text[-h:]}"


class Approver:
    """Asks before risky calls. `auto=True` approves everything (--permissions yes)."""

    # Commands that can't run other programs or write files, whatever their flags.
    SAFE = {"ls", "cat", "head", "tail", "wc", "pwd", "grep", "echo", "which", "stat", "diff"}
    SHELL = set(";&|<>`$(){}\n\r\\")

    def __init__(self, auto: bool = False, interactive: bool = True, allow: tuple = (), use_judge: bool = False):
        self.auto, self.interactive = auto, interactive
        self.use_judge, self.judge, self.reason = use_judge, None, ""  # judge(kind, detail) -> (allow|deny|ask, why)
        self.allow = [shlex.split(a) for a in allow if a.strip()]

    def pre_approved(self, command: str) -> bool:
        if self.SHELL & set(command):
            return False
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        return bool(words) and (words[0] in self.SAFE or any(words[:len(a)] == a for a in self.allow))

    def ok(self, kind: str, detail: str) -> bool:
        if self.auto or (kind == "bash" and self.pre_approved(detail)):
            return True
        if kind == "mcp" and any(detail.split()[0].startswith(a[0]) for a in self.allow):
            return True
        self.reason = ""
        if len(detail) > 4000:  # too long to review, by you or the judge, which must see the whole action
            self.reason = " Too long to review: split it into smaller steps."
            return False
        if self.use_judge and self.judge and kind != "write":  # gated writes run later: the user decides
            verdict, why = self.judge(kind, detail)
            self.reason = f" Judge: {why}"
            if verdict != "ask":
                return verdict == "allow"
        if not self.interactive:
            return False
        # Every row carries a marker content can't fake, and nothing taller than a screen is shown at all.
        cols = max(shutil.get_terminal_size().columns - 6, 20)
        rows = [r[i:i + cols] for r in printable(detail, kind == "write").split("\n") for i in range(0, len(r) or 1, cols)]
        if len(rows) > max(min(40, shutil.get_terminal_size().lines - 3), 10):  # the top must stay on screen
            self.reason = " Too long to review on one screen: split it, or write a short file and extend it with edit."
            return False
        head = printable(detail.split("\n", 1)[0] if kind == "write" else kind)  # the path, by the answer
        return input(f"\033[!p\033[r\033[0m\033[?7h\033[33m  allow {kind}:" + "".join(f"\n  \u2502 {r}" for r in rows)
                     + f"\n  [{head} · {len(rows)} rows · y/n] \033[0m").strip().lower() in ("y", "yes")


class LocalTools:
    def __init__(self, cwd: str, approver: Approver):
        self.cwd = Path(cwd).resolve()
        self.approver = approver
        self.seen: dict[Path, float] = {}  # path -> mtime when the model last read or wrote it

    def _path(self, p: str) -> Path:
        path = Path(p).expanduser()
        return (path if path.is_absolute() else self.cwd / path).resolve()

    # Files something else executes later; "head" and "config" because git treats any folder with
    # HEAD, config, objects/ and refs/ as a repository and runs hooks from its config.
    # This is a denylist: it covers common runners, not every tool that reads a file from the project.
    PROTECTED = {"mcp.json", "conftest.py", "makefile", "gnumakefile", "justfile", "package.json", "pyproject.toml",
                 "setup.py", "noxfile.py", "tox.ini", "head", "config"}

    def _inside(self, path: Path) -> bool:
        """Free to write: inside the working directory, and not a file something else will execute later
        (dot-folders such as .git hooks and config, MCP config, test or build config). macOS paths ignore case."""
        home = Path.home().resolve()
        if self.cwd not in path.parents or self.cwd == home or self.cwd in home.parents:
            return False
        parts = [x.casefold() for x in path.relative_to(self.cwd).parts]
        return not any(x.startswith(".") for x in parts) and parts[-1] not in self.PROTECTED

    def _ask(self, path: Path) -> bool:
        """Writes need approval outside the project, into protected files, or over an executable."""
        return not self._inside(path) or (path.exists() and os.access(path, os.X_OK))

    def read(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        p = self._path(path)
        if p.is_dir():
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))
        if not p.is_file():
            raise ToolError("Not a regular file.")
        if p.stat().st_size > 5_000_000:
            raise ToolError("File is over 5 MB. Use bash (head, tail, grep) to look at parts of it.")
        lines = p.read_text(errors="replace").splitlines()
        self.seen[p] = p.stat().st_mtime
        start, shown, size = max(offset, 1), [], 0
        for i, line in enumerate(lines[start - 1:start - 1 + max(limit, 1)], start):
            numbered = f"{i:>5}\t{line}"
            if shown and size + len(numbered) > MAX_OUT - 200:  # page here, never let the output cap cut the middle
                break
            shown.append(numbered)
            size += len(numbered) + 1
        end = start + len(shown) - 1
        more = f"\n[lines {start}-{end} of {len(lines)}; continue with offset={end + 1}]" if end < len(lines) else ""
        return "\n".join(shown) + more

    def write(self, path: str, content: str) -> str:
        p = self._path(path)
        if p.exists() and p.stat().st_mtime != self.seen.get(p):
            raise ToolError("File exists and you haven't read its current version. Read it first, or use edit.")
        if self._ask(p) and not self.approver.ok("write", f"{p}\n{content}"):
            raise ToolError("User denied this write (outside the project, or a file that runs later)." + self.approver.reason)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            with p.open(newline="") as f:
                if "\r\n" in f.read():  # keep a Windows file's line endings
                    content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        p.write_text(content, newline="")
        self.seen[p] = p.stat().st_mtime
        return f"Wrote {len(content.splitlines())} lines to {p}"

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = self._path(path)
        if p not in self.seen:
            raise ToolError("Read the file before editing it.")
        if p.stat().st_mtime != self.seen[p]:
            raise ToolError("File changed since you last read it. Read it again.")
        if self._ask(p) and not self.approver.ok("write", "\n".join(
                [str(p)] + ["- " + x for x in old.split("\n")] + ["+ " + x for x in new.split("\n")])):
            raise ToolError("User denied this edit (outside the project, or a file that runs later)." + self.approver.reason)
        if not old:
            raise ToolError("`old` must not be empty.")
        with p.open(newline="") as f:  # keep the file's own line endings
            text = f.read()
        if old not in text and "\r\n" in text:  # the model writes \n; the file uses \r\n
            old = old.replace("\r\n", "\n").replace("\n", "\r\n")
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        n = text.count(old)
        if n == 0:
            raise ToolError("`old` not found. It must match the file exactly, including whitespace.")
        if n > 1 and not replace_all:
            raise ToolError(f"`old` appears {n} times. Add context to make it unique, or set replace_all.")
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), newline="")
        self.seen[p] = p.stat().st_mtime
        return f"Edited {p} ({n if replace_all else 1} replaced)"

    def find(self, name: str = "*", contains: str = "", path: str = ".") -> str:
        """Search with no shell, so it never needs approval. Skips dot-folders and dependency folders."""
        root, rx, hits = self._path(path), re.compile(contains) if contains else None, []  # bad regex: re.error
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__"))
            for p in (Path(d, f) for f in sorted(fnmatch.filter(files, name))):
                text = p.read_text(errors="replace") if rx and p.is_file() and p.stat().st_size < 5_000_000 else ""
                if not rx or "\0" not in text[:8000]:  # skip binaries
                    hits += [f"{p.relative_to(root)}:{i}: {x[:300]}" for i, x in enumerate(text.splitlines(), 1)
                             if rx.search(x)] if rx else [str(p.relative_to(root))]
            if len(hits) >= 500:
                return "\n".join(hits[:500]) + "\n[stopped at 500 results: narrow the search]"
        return "\n".join(hits) or "[no matches]"

    def bash(self, command: str, timeout: int = 120) -> str:
        if not self.approver.ok("bash", command):
            raise ToolError("User denied this command." + self.approver.reason)
        try:
            code, stdout, stderr = run_shell(command, self.cwd, max(timeout, 1))
        except subprocess.TimeoutExpired:
            raise ToolError(f"Timed out after {timeout}s.")
        out = (stdout + (("\n[stderr]\n" + stderr) if stderr else "")).strip()
        return f"{out}\n[exit {code}]" if code else (out or "[no output]")


def schema(props: dict, required: list) -> dict:
    """strict: the API guarantees inputs match the schema, so calls never fail on argument shape."""
    return {"strict": True, "input_schema": {"type": "object", "properties": props, "required": required,
                                             "additionalProperties": False}}


SCHEMAS = {
    "read": {
        "description": "Read a text file, or list a directory. Paths are relative to the working directory. Output "
                       "lines start with a line number and a tab; that prefix is not part of the file. Long files "
                       "come in pages of about 30,000 characters; the last line says which offset to pass next.",
        **schema({"path": {"type": "string"},
                  "offset": {"type": "integer", "description": "First line to show, 1-based (default 1)"},
                  "limit": {"type": "integer", "description": "Lines to show (default 2000)"}}, ["path"])},
    "write": {
        "description": "Create a file, or replace a whole file. To overwrite an existing file, read it first; for "
                       "small changes use edit. Writing outside the working directory, into dot-folders, or to "
                       "files that run later (Makefile, conftest.py, mcp.json, executables) asks the user, who may say no.",
        **schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    "edit": {
        "description": "Replace exact text in a file you have read since it last changed. `old` must match the file "
                       "character for character, including indentation, and must not include the line-number "
                       "prefixes that read shows. It must occur once, unless replace_all is true; add surrounding "
                       "lines to make it unique.",
        **schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                  "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"}},
                 ["path", "old", "new"])},
    "bash": {
        "description": "Run a shell command in the working directory and get stdout, stderr and the exit code. Use "
                       "it for git, tests and builds; search with find. There is no terminal: commands get no "
                       "input, so interactive programs end at once. Output over 30,000 characters is cut in the "
                       "middle. Most commands ask the user first and can be denied; plain reads such as ls, cat, "
                       "grep and head without pipes or redirects run without asking.",
        **schema({"command": {"type": "string"},
                  "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 120)"}},
                 ["command"])},
    "find": {
        "description": "Find files by name, or lines by content (path:line: text), under a folder. Read-only, never "
                       "asks the user. Skips dot-folders, node_modules and __pycache__. Stops at 500 results.",
        **schema({"name": {"type": "string", "description": "Filename glob, such as *.py or test_*; default *"},
                  "contains": {"type": "string", "description": "Python regex to match lines; omit to list files"},
                  "path": {"type": "string", "description": "Folder to search (default the working directory)"}}, [])},
}

WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search"}
