"""Sessions: one append-only JSONL file per conversation.

The file is the transcript, the resume source and the audit log. Nothing in it
is ever rewritten. A compaction starts a new segment: on load, everything
before the latest `compact` event is skipped.
"""
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

HOME = Path(os.environ.get("MINI_HOME", Path.home() / ".mini"))


def project_dir(cwd: str) -> Path:
    key = hashlib.sha256(os.path.realpath(cwd).encode()).hexdigest()[:12]
    d = HOME / "sessions" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.id = path.stem

    @classmethod
    def create(cls, cwd: str, name: str | None = None) -> "Session":
        sid = name or time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        return cls(project_dir(cwd) / f"{sid}.jsonl")

    @classmethod
    def find(cls, cwd: str, sid: str | None = None) -> "Session | None":
        """Latest session, or the latest one whose id starts with `sid`."""
        files = [f for f in sessions(cwd) if f.stem.startswith(sid or "")]
        return cls(files[0]) if files else None

    def append(self, kind: str, **data):
        with self.path.open("a+b") as f:
            if f.tell() and (f.seek(-1, 2), f.read(1))[1] != b"\n":  # a crash cut the last line; don't glue onto it
                f.write(b"\n")
            f.write((json.dumps({"t": time.time(), "kind": kind, **data}) + "\n").encode())

    def events(self):
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:  # a line cut off by a crash; everything before it is intact
                pass
        return events

    def load(self):
        """Return (meta, messages, goal, cost, context size) rebuilt from the log."""
        meta, messages, goal, cost, context = None, [], None, 0.0, 0
        for e in self.events():
            if e["kind"] == "meta":
                meta = e
            elif e["kind"] == "msg":
                messages.append(e["message"])
            elif e["kind"] == "msgs":  # several messages that only make sense together
                messages.extend(e["messages"])
            elif e["kind"] == "compact":
                messages, context = list(e.get("messages") or [e["message"]]), 0
            elif e["kind"] == "goal":
                goal = e["goal"] if e["goal"]["status"] != "cleared" else None
            elif e["kind"] == "usage":
                cost += e.get("cost", 0)
                context = e.get("context", context)
        return meta, messages, goal, cost, context


def sessions(cwd: str) -> list[Path]:
    """Top-level sessions for this directory, newest first (subagent logs excluded)."""
    files = [p for p in project_dir(cwd).glob("*.jsonl") if "-sub" not in p.stem]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def list_sessions(cwd: str, limit: int = 15):
    rows = []
    for p in sessions(cwd)[:limit]:
        first, turns, cost = "", 0, 0.0
        for e in Session(p).events():
            if e["kind"] == "msg" and e["message"]["role"] == "user":
                c = e["message"]["content"]
                if isinstance(c, str):
                    turns += 1
                    first = first or c
            elif e["kind"] == "usage":
                cost += e.get("cost", 0)
        rows.append((p.stem, turns, cost, first.replace("\n", " ")[:60]))
    return rows
