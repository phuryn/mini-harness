"""Talking to it: a readline REPL with six slash commands, or headless with -p."""
import argparse
import json
import os
import sys

from .ext import MCPHub, find_skills
from .loop import Agent
from .session import Session, list_sessions
from .tools import Approver

DIM, YEL, CYA, RED, OFF = "\033[2m", "\033[33m", "\033[36m", "\033[31m", "\033[0m"

HELP = """/new                 start a fresh session
/sessions            list recent sessions
/resume [id]         resume the latest session, or one by id prefix
/compact             replace the history with a summary now
/goal <objective>    work until done · /goal resume|clear|status
/exit                quit (sessions save after every message)"""


class UI:
    def __init__(self, indent="", quiet=False):
        self.indent, self.quiet, self.mode = indent, quiet, None

    def _switch(self, mode):
        if self.mode != mode:
            sys.stdout.write("\n" if self.mode else "")
            self.mode = mode

    def text(self, s):
        if not self.quiet:
            self._switch("text")
            sys.stdout.write(s)
            sys.stdout.flush()

    def thinking(self, s):
        if not self.quiet:
            self._switch("think")
            sys.stdout.write(DIM + s + OFF)
            sys.stdout.flush()

    def end_message(self, stop, agent):
        if self.mode:
            sys.stdout.write("\n")
        self.mode = None

    def tool_call(self, name, args):
        brief = args.get("command") or args.get("path") or args.get("prompt") or json.dumps(args)[:120]
        print(f"{self.indent}{CYA}→ {name}{OFF} {str(brief).splitlines()[0][:150] if brief else ''}")

    def tool_result(self, out, err):
        first = (out.strip().splitlines() or [""])[0][:150]
        print(f"{self.indent}  {RED if err else DIM}{first}{OFF}")

    def note(self, s):
        print(f"{self.indent}{DIM}· {s}{OFF}")

    def warn(self, s):
        print(f"{self.indent}{YEL}! {s}{OFF}")

    def child(self):
        return UI(self.indent + "    ", quiet=True)


def status_line(agent):
    return f"{DIM}[{agent.session.id} · {agent.last_context:,} tokens in context · ${agent.cost:.3f} total]{OFF}"


def print_goal(g):
    if not g:
        print("No goal.")
    else:
        print(f"Goal [{g['status']}] {g['objective']}\n  turns {g['turns']}/{g['max_turns']} · ${g['spent']:.3f}"
              + (f" · {g['note']}" if g.get("note") else ""))


def main():
    p = argparse.ArgumentParser(prog="mini", description="A minimal agent harness.")
    p.add_argument("-p", "--prompt", help="run one prompt headless and print the answer")
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the latest session")
    p.add_argument("-r", "--resume", help="resume a session by id prefix")
    p.add_argument("--model", help="main model (default claude-opus-5-5)")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--yes", action="store_true", help="approve every tool call")
    p.add_argument("--allow", action="append", default=[], metavar="PREFIX",
                   help="approve bash commands starting with PREFIX (repeatable); others are denied when headless")
    p.add_argument("--goal", help="headless: work until this objective is done")
    p.add_argument("--verify", help="with --goal: command that must pass before the goal counts as done")
    p.add_argument("--max-turns", type=int, default=25)
    p.add_argument("--budget", type=float, help="with --goal: stop after this many dollars")
    p.add_argument("--json", action="store_true", help="headless: print a JSON result line at the end")
    args = p.parse_args()

    cwd = os.getcwd()
    headless = bool(args.prompt or args.goal)
    ui = UI(quiet=headless and args.json)
    approver = Approver(auto=args.yes, interactive=not headless, allow=args.allow)
    hub, skills = MCPHub(cwd), find_skills(cwd)
    for e in hub.errors:
        ui.warn(f"MCP {e}")

    session = None
    if args.cont or args.resume:
        session = Session.find(cwd, args.resume)
        if not session:
            sys.exit("No session found.")
    new = lambda s: Agent(cwd, s, ui, approver, hub=hub, skills=skills, model=args.model, effort=args.effort)
    agent = new(session or Session.create(cwd))

    try:
        if headless:
            if args.goal:
                g = agent.set_goal(args.goal, args.verify, args.max_turns, args.budget)
                result = {"status": g["status"], "note": g["note"], "turns": g["turns"]}
            else:
                result = {"stop": agent.turn(args.prompt)}
            if args.json:
                print(json.dumps({**result, "session": agent.session.id, "cost": round(agent.cost, 4),
                                  "answer": agent.final_text()}))
            else:
                print(status_line(agent))
            return
        repl(agent, new)
    finally:
        hub.close()


def repl(agent, new):
    try:
        import readline  # noqa: F401  (line editing and history)
    except ImportError:
        pass
    print(f"{DIM}mini · {agent.model} · session {agent.session.id} · /help for commands{OFF}")
    while True:
        try:
            line = input(f"\n{CYA}›{OFF} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line == "/exit":
            return
        try:
            agent = command(agent, line, new) if line else agent
        except KeyboardInterrupt:
            print(f"{YEL}! interrupted{OFF}")
        except Exception as e:  # keep the REPL alive; the session file is still valid
            print(f"{RED}! {type(e).__name__}: {e}{OFF}")
        print(status_line(agent))


def command(agent, line, new):
    cmd, _, rest = line.partition(" ")
    rest = rest.strip()
    cwd = os.getcwd()
    if cmd == "/help":
        print(HELP)
    elif cmd == "/new":
        agent = new(Session.create(cwd))
        print(f"{DIM}new session {agent.session.id}{OFF}")
    elif cmd == "/sessions":
        for sid, turns, cost, first in list_sessions(cwd):
            print(f"  {sid}  {turns:>3} prompts  ${cost:>7.3f}  {first}")
    elif cmd == "/resume":
        s = Session.find(cwd, rest or None)
        if not s:
            print("No session found.")
            return agent
        agent = new(s)
        print(f"{DIM}resumed {agent.session.id} ({len(agent.messages)} messages){OFF}")
        if agent.goal:
            print_goal(agent.goal)
    elif cmd == "/compact":
        if agent.compact():
            print(f"{DIM}compacted{OFF}")
    elif cmd == "/goal":
        if rest in ("", "status"):
            print_goal(agent.goal)
        elif rest == "clear":
            agent.clear_goal()
            print("Goal cleared.")
        elif rest == "resume":
            if agent.goal and agent.goal["status"] in ("paused", "active"):
                print_goal(agent.run_goal())
            else:
                print("No paused goal.")
        else:
            print_goal(agent.set_goal(rest))
    elif cmd.startswith("/"):
        print(f"Unknown command {cmd}. /help lists them.")
    else:
        agent.maybe_compact()
        agent.turn(line)
    return agent


if __name__ == "__main__":
    main()
