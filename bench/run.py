"""Run each benchmark task in a fresh copy of its folder, in mini and in Claude Code, and record the results.

Usage: uv run python bench/run.py [--harness mini|mini-deepseek|claude|both] [--runs N] [--only id,id]
Writes one JSON line per run to bench/results.jsonl.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
VENV_BIN = str(Path(sys.executable).parent)  # so python3 in tasks has pytest


def run_mini(task, work, env, model=None):
    cmd = ["mini", "--json"] + (["--model", model] if model else [])
    for a in task["allow"]:
        cmd += ["--allow", a]
    if task.get("goal"):
        cmd += ["--goal", task["prompt"], "--verify", task["check"], "--max-turns", "8"]
    else:
        cmd += ["-p", task["prompt"]]
    r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=1800)
    last = (r.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        out = json.loads(last)
    except json.JSONDecodeError:
        out = {"error": (r.stderr or r.stdout)[-2000:]}
    return {"cost": out.get("cost"), "session": out.get("session"), "status": out.get("status") or out.get("stop"),
            "error": out.get("error")}


def run_claude(task, work, env):
    allowed = ["Read", "Write", "Edit", "Glob", "Grep"] + [f"Bash({a}:*)" for a in task["allow"]]
    prompt = task["prompt"] + (f"\n\nKeep going until `{task['check']}` passes." if task.get("goal") else "")
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", "claude-opus-5-5",
           "--allowedTools", ",".join(allowed)]
    r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=1800)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        out = {"error": (r.stderr or r.stdout)[-2000:]}
    return {"cost": out.get("total_cost_usd"), "turns": out.get("num_turns"), "error": out.get("error")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", default="both", choices=["mini", "mini-deepseek", "claude", "both"])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only")
    args = ap.parse_args()
    tasks = json.loads((ROOT / "tasks.json").read_text())
    if args.only:
        tasks = [t for t in tasks if t["id"] in args.only.split(",")]
    harnesses = ["mini", "claude"] if args.harness == "both" else [args.harness]
    for task in tasks:
        for h in harnesses:
            for n in range(args.runs):
                work = Path(tempfile.mkdtemp(prefix=f"bench-{task['id']}-"))
                shutil.copytree(ROOT / "tasks" / task["id"], work, dirs_exist_ok=True)
                env = {**os.environ, "PATH": VENV_BIN + os.pathsep + os.environ["PATH"],
                       "MINI_HOME": str(ROOT / ".runs" / "home"), **task.get("env", {})}
                t0 = time.time()
                res = (run_claude(task, work, env) if h == "claude" else
                       run_mini(task, work, env, "deepseek-flash" if h == "mini-deepseek" else None))
                secs = round(time.time() - t0, 1)
                ok = subprocess.run(task["check"], shell=True, cwd=work, env=env, capture_output=True).returncode == 0
                row = {"task": task["id"], "harness": h, "run": n, "passed": ok, "seconds": secs, **res,
                       "workdir": str(work)}
                print(json.dumps(row), flush=True)
                with (ROOT / "results.jsonl").open("a") as f:
                    f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
