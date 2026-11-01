"""Talking to it: a readline REPL with nine slash commands, or headless with -p."""
import argparse
import json
import os
import sys

import anthropic

from .ext import MCPHub, find_skills
from .loop import OTHER, Agent
from .session import Session, list_sessions
from .tools import Approver, printable

DIM, YEL, CYA, RED, OFF = "\033[2m", "\033[33m", "\033[36m", "\033[31m", "\033[0m"

HELP = """/new [model]         start a fresh session, on another model if given (see mini --models)
/effort [level]      show or change the effort for the next requests
/permissions [mode]  ask, judge or yes: who approves risky calls
/sessions · /resume [id]  list sessions here · resume the latest, or one by id prefix
/goal <objective>    work until done · /goal resume|clear|status
/compact · /exit     summarize the history now · quit (every message is already saved)"""


class UI:
    def __init__(self, indent="", quiet=False, silent=False):
        """quiet hides streamed text and thinking (subagents); silent hides everything but warnings (--json)."""
        self.indent, self.quiet, self.silent, self.mode = indent, quiet or silent, silent, None

    def _out(self, mode, s):
        if not self.quiet:
            sys.stdout.write(("\n" if self.mode and self.mode != mode else "") + s)
            sys.stdout.flush()
            self.mode = mode

    def text(self, s):
        self._out("text", s)

    def thinking(self, s):
        self._out("think", DIM + s + OFF)

    def end_message(self):
        if self.mode:
            sys.stdout.write("\n")
        self.mode = None

    def _say(self, s):
        if not self.silent:
            print(self.indent + s)

    def tool_call(self, name, args):
        brief = args.get("command") or args.get("path") or args.get("prompt") or json.dumps(args)[:120]
        self._say(f"{CYA}→ {name}{OFF} {printable(str(brief).splitlines()[0][:150]) if brief else ''}")

    def tool_result(self, out, err):
        self._say(f"  {RED if err else DIM}{printable((out.strip().splitlines() or [''])[0][:150])}{OFF}")

    def note(self, s):
        self._say(f"{DIM}· {s}{OFF}")

    def warn(self, s):
        print(f"{self.indent}{YEL}! {s}{OFF}", file=sys.stderr)

    def child(self):
        return UI(self.indent + "    ", quiet=True, silent=self.silent)


def status_line(agent):
    # right after compaction the size is unknown until the next reply reports it
    ctx = ("context just compacted" if agent.messages and not agent.last_context
           else f"{agent.last_context:,} of {agent.window:,} tokens in context")
    return f"{DIM}[{agent.session.id} · {ctx} · ${agent.cost:.3f} total]{OFF}"


def print_goal(g):
    print("No goal." if not g else f"Goal [{g['status']}] {g['objective']}\n  {g['turns']} turns ({g['max_turns']} per run) · ${g['spent']:.3f}"
          + (f" · {g['note']}" if g.get("note") else ""))


def main():
    p = argparse.ArgumentParser(prog="mini", description="A minimal agent harness.")
    p.add_argument("-p", "--prompt", help="run one prompt headless and print the answer")
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the latest session")
    p.add_argument("-r", "--resume", help="resume a session by id prefix")
    p.add_argument("--model", help="main model (default claude-opus-5-5)")
    p.add_argument("--effort", default="high", help="low, medium, high, xhigh or max (DeepSeek: low, high, max)")
    p.add_argument("--models", action="store_true", help="list the models you can use, then exit")
    p.add_argument("--permissions", default="ask", choices=["ask", "judge", "yes"],
                   help="who approves risky calls: you, Jev (a decision model; needs OPENROUTER_API_KEY), or nobody")
    p.add_argument("--allow", action="append", default=[], metavar="PREFIX",
                   help="pre-approve bash commands starting with these words, or MCP tools starting with PREFIX "
                        "(repeatable). Allowing a test runner lets the model run code it wrote")
    p.add_argument("--goal", help="headless: work until this objective is done")
    p.add_argument("--verify", help="with --goal: command that must pass before the goal counts as done")
    p.add_argument("--max-turns", type=int, default=25)
    p.add_argument("--budget", type=float, help="with --goal: stop after this many dollars")
    p.add_argument("--json", action="store_true", help="headless: print a JSON result line at the end")
    args = p.parse_args()
    if args.models:
        if os.environ.get("ANTHROPIC_API_KEY"):
            print(*[m.id for m in anthropic.Anthropic().models.list()], sep="\n")
        print(*OTHER, sep="\n")
        return

    cwd = os.getcwd()
    headless = bool(args.prompt or args.goal)
    ui = UI(silent=headless and args.json)
    approver = Approver(auto=args.permissions == "yes", interactive=not headless, allow=args.allow,
                        use_judge=args.permissions == "judge")
    hub, skills = MCPHub(cwd), find_skills(cwd)
    for e in hub.errors:
        ui.warn(f"MCP {e}")

    session = None
    if args.cont or args.resume:
        session = Session.find(cwd, args.resume)
        if not session:
            sys.exit("No session found.")
    def new(s, model=None):
        agent = Agent(cwd, s, ui, approver, hub=hub, skills=skills, model=model or args.model, effort=args.effort)
        approver.judge = agent.judge
        if agent.effort != args.effort:
            ui.warn(f"{agent.model} doesn't take effort {args.effort}; using {agent.effort} ({', '.join(agent.efforts)})")
        agent.goal_defaults = {"verify": args.verify, "max_turns": args.max_turns, "budget": args.budget}
        return agent

    try:
        agent = new(session or Session.create(cwd))
        if headless:
            if args.goal:
                g = agent.set_goal(args.goal, args.verify, args.max_turns, args.budget)
                result = {"status": g["status"], "note": g["note"], "turns": g["turns"]}
            else:
                agent.maybe_compact()
                result = {"stop": agent.turn(args.prompt)}
            if args.json:
                print(json.dumps({**result, "session": agent.session.id, "cost": round(agent.cost, 4),
                                  "answer": agent.final_text()}))
            else:
                print(status_line(agent))
            return
        repl(agent, new)
    except RuntimeError as e:  # e.g. a missing API key
        sys.exit(str(e))
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
        agent = new(Session.create(cwd), rest or None)
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
    elif cmd == "/permissions":
        if rest in ("ask", "judge", "yes"):
            agent.approver.auto, agent.approver.use_judge = rest == "yes", rest == "judge"
        print(f"permissions {'yes' if agent.approver.auto else 'judge' if agent.approver.use_judge else 'ask'} · ask, judge or yes")
    elif cmd == "/effort":
        if rest and rest not in agent.efforts:
            print(f"{agent.model} has no effort {rest}")
        agent.effort = rest if rest in agent.efforts else agent.effort
        print(f"effort {agent.effort} · {agent.model} takes {', '.join(agent.efforts)}")
    elif cmd == "/compact":
        agent.compact()
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
            print_goal(agent.set_goal(rest, agent.goal_defaults.get("verify"), agent.goal_defaults.get("max_turns", 25),
                                      agent.goal_defaults.get("budget")))
    elif cmd.startswith("/"):
        print(f"Unknown command {cmd}. /help lists them.")
    else:
        agent.maybe_compact()
        agent.turn(line)
    return agent


if __name__ == "__main__":
    main()
