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
        """Latest session, or one whose id starts with `sid`."""
        files = sorted(project_dir(cwd).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        files = [f for f in files if "-sub" not in f.stem]
        if sid:
            files = [f for f in files if f.stem.startswith(sid)]
        return cls(files[-1]) if files else None

    def append(self, kind: str, **data):
        with self.path.open("a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, **data}) + "\n")

    def events(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def load(self):
        """Return (meta, messages, goal) rebuilt from the log."""
        meta, messages, goal = None, [], None
        for e in self.events():
            if e["kind"] == "meta":
                meta = e
            elif e["kind"] == "msg":
                messages.append(e["message"])
            elif e["kind"] == "compact":
                messages = [e["message"]]
            elif e["kind"] == "goal":
                goal = e["goal"]
        return meta, messages, goal


def list_sessions(cwd: str, limit: int = 15):
    rows = []
    for p in sorted(project_dir(cwd).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if "-sub" in p.stem:
            continue
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
        if len(rows) >= limit:
            break
    return rows
