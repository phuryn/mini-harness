"""The agent loop: call the model, run the tools it asks for, repeat.

Three rules keep this loop valid on current Claude models:
1. Append the model's content blocks as returned (thinking signatures,
   compaction blocks), never just the text. The one exception is the partial
   that a declining model left before a server-side fallback (see clean()).
2. Never edit what was already sent. System prompt and tools are frozen per
   session; later instructions are appended as role:"system" messages.
3. Every tool_use gets a tool_result in the next user message, even when the
   call was denied, failed or interrupted.
"""
import json
import os
import secrets
import subprocess

import anthropic

from .context import build_system
from .session import Session
from .tools import SCHEMAS, WEB_SEARCH, LocalTools, ToolError, run_shell, truncate

MAIN_MODEL = os.environ.get("MINI_MODEL", "claude-opus-5-5")
SUB_MODEL = "claude-sonnet-5-5"
COMPACT_AT = int(os.environ.get("MINI_COMPACT_AT", 150_000))  # input tokens
BETAS = ["compact-2026-09-04", "thinking-binding-controls-2026-08-01"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# $ per million tokens: input, output, cache read, 5-minute cache write. Fallback targets included.
# Haiku 5.5 cache rates assume the usual 0.1x / 1.25x multipliers (prompts under 100K tokens).
PRICES = {"claude-opus-5-5": (4, 20, 0.20, 5), "claude-sonnet-5-5": (2, 10, 0.20, 2.5),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125), "claude-opus-5": (5, 25, 0.50, 6.25),
          "claude-opus-4-8": (5, 25, 0.50, 6.25), "claude-sonnet-5": (2, 10, 0.20, 2.5)}
WEB_SEARCH_PRICE = 0.01  # $ per search

GOAL_RULES = (
    "Goal mode is on. Objective: {objective}\n"
    "The harness will keep asking you to continue until you call goal_done. Call goal_done only when the objective is "
    "fully met and you have checked it, and put the evidence (commands run, results) in the call. {verify}"
    "If you cannot make progress without the user, end your message with a line that starts with BLOCKED: and the question.")

GOAL_OFF = "Goal mode is off (status: {status}). Earlier goal instructions no longer apply unless the user resumes the goal."

EXTRA_SCHEMAS = {
    "task": {
        "description": "Delegate a self-contained research or reading task to a subagent with a fresh context and "
                       "read, bash and web_search. It returns only its final answer, so give it everything it needs.",
        "input_schema": {"type": "object", "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string", "enum": ["claude-sonnet-5-5", "claude-haiku-5-5"],
                      "description": "Default claude-sonnet-5-5; haiku for plain reading or search"}},
            "required": ["prompt"]}},
    "goal_done": {
        "description": "Declare the active goal complete. Only call when it is fully met and checked.",
        "input_schema": {"type": "object", "properties": {"evidence": {"type": "string"}}, "required": ["evidence"]}},
}


def n(x) -> int:
    return x or 0


def clean(blocks: list[dict]) -> list[dict]:
    """After a mid-stream fallback, drop the declined model's unpaired calls and thinking before the boundary."""
    cut = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=-1)
    answered = {b.get("tool_use_id") for b in blocks}
    keep = lambda b: b["type"] == "text" or b["type"].endswith("_tool_result") or (
        b["type"] == "server_tool_use" and b["id"] in answered)
    return [b for b in blocks[:max(cut, 0)] if keep(b)] + blocks[max(cut, 0):]


class Agent:
    def __init__(self, cwd, session: Session, ui, approver, hub=None, skills=(), model=None, effort="high", sub=False,
                 client=None):
        self.cwd, self.session, self.ui, self.approver, self.hub = cwd, session, ui, approver, hub
        self.client = client or anthropic.Anthropic()
        self.local = LocalTools(cwd, approver)
        self.effort = effort
        self.pending_system: list[str] = []
        self.goal_claim = None
        meta, self.messages, self.goal, self.cost, self.last_context = session.load()
        if meta:  # resume: replay the frozen prompt and tools byte for byte
            self.model, self.system, self.tools = meta["model"], meta["system"], meta["tools"]
            self.repair()
            said = "\n".join(m["content"] for m in self.messages if m["role"] == "system")
            if said.rfind("Goal mode is on") > said.rfind("Goal mode is off") and not (self.goal and self.goal["status"] == "active"):
                self.pending_system.append(GOAL_OFF.format(status=self.goal["status"] if self.goal else "cleared"))
        else:
            self.model = model or (SUB_MODEL if sub else MAIN_MODEL)
            names = ["read", "bash"] if sub else ["read", "write", "edit", "bash"]
            self.tools = [{"name": x, **SCHEMAS[x]} for x in names]
            if not sub:
                self.tools += [{"name": x, **s} for x, s in EXTRA_SCHEMAS.items()]
                self.tools += hub.schemas if hub else []
            self.tools.append(WEB_SEARCH)
            self.system = build_system(cwd, list(skills), hub.errors if hub else [], sub)
            session.append("meta", model=self.model, cwd=str(cwd), system=self.system, tools=self.tools)

    # --- transcript -------------------------------------------------------
    def add(self, message: dict):
        self.messages.append(message)
        self.session.append("msg", message=message)

    def add_user(self, content):
        self.add({"role": "user", "content": content})
        if self.pending_system:  # one system message, right after a user message
            self.add({"role": "system", "content": "\n\n".join(dict.fromkeys(self.pending_system))})
        self.pending_system = []

    def placeholder(self, why: str):
        self.add({"role": "assistant", "content": [{"type": "text", "text": f"({why})"}]})

    def repair(self):
        """A session that died mid-turn gets its tail closed, by appending only."""
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "assistant":
            uses = [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
            if uses:
                self.add_user([{"type": "tool_result", "tool_use_id": u["id"], "is_error": True,
                                "content": "Not run: the session ended before this call."} for u in uses])
                last = self.messages[-1]
        if last and last["role"] in ("user", "system"):
            self.placeholder("the previous session ended here")

    def final_text(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                return "\n".join(b["text"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "text")
        return ""

    # --- model calls ------------------------------------------------------
    def request_args(self) -> dict:
        kw = dict(model=self.model, max_tokens=64000, system=[{"type": "text", "text": self.system}],
                  tools=self.tools, messages=self.messages, cache_control={"type": "ephemeral"},
                  thinking={"type": "adaptive", "display": "summarized",
                            "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
                  output_config={"effort": self.effort}, betas=list(BETAS))
        if "haiku" not in self.model:  # refusals fall back to another model server-side
            kw["fallbacks"] = "default"
            kw["betas"].append(FALLBACK_BETA)
        return kw

    def call(self):
        with self.client.beta.messages.stream(**self.request_args()) as stream:
            for ev in stream:
                if ev.type == "content_block_delta":
                    if ev.delta.type == "text_delta":
                        self.ui.text(ev.delta.text)
                    elif ev.delta.type == "thinking_delta":
                        self.ui.thinking(ev.delta.thinking)
            return stream.get_final_message()

    def track(self, resp):
        u = resp.usage
        cost = 0.0
        for i in (u.iterations or [u]):  # each iteration (compaction, declined attempt, fallback) has its own price
            model = getattr(i, "model", None) or resp.model
            pin, pout, pread, pwrite = PRICES.get(model) or PRICES["claude-opus-5-5"]
            if "haiku-5-5" in model and n(i.input_tokens) + n(i.cache_read_input_tokens) > 100_000:
                pin, pout, pread, pwrite = 0.50, 2.50, 0.05, 0.625  # long-prompt rate card
            cost += (n(getattr(i, "input_tokens", 0)) * pin + n(getattr(i, "output_tokens", 0)) * pout
                     + n(getattr(i, "cache_read_input_tokens", 0)) * pread
                     + n(getattr(i, "cache_creation_input_tokens", 0)) * pwrite) / 1e6
        if u.server_tool_use:
            cost += n(u.server_tool_use.web_search_requests) * WEB_SEARCH_PRICE
        self.cost += cost
        last = next((i for i in reversed(u.iterations or []) if i.type in ("message", "fallback_message")), u)
        self.last_context = sum(n(getattr(last, f, 0)) for f in
                                ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
        self.session.append("usage", usage=u.to_dict(exclude_none=True), cost=round(cost, 6), context=self.last_context)
        drops = [t.to_dict() for t in (resp.input_transformations or [])]
        if drops:
            self.session.append("transform", items=drops)
            if any(d.get("reason") == "prefix_binding_mismatch" for d in drops):  # model switches are expected
                self.ui.warn(f"API dropped thinking: history was edited before {drops[0].get('path')}")

    # --- one user turn ----------------------------------------------------
    def turn(self, content) -> str:
        """Run until the model stops asking for tools. Returns the stop reason."""
        self.goal_claim = None
        self.repair()  # closes anything an earlier Ctrl-C left open; only appends
        self.add_user(content)
        while True:
            try:
                resp = self.call()
            except KeyboardInterrupt:
                self.placeholder("interrupted by the user")
                self.ui.warn("interrupted")
                return "interrupted"
            except Exception as e:  # the transcript must stay valid whatever failed
                self.placeholder(f"request failed: {type(e).__name__}")
                self.ui.warn(f"request failed: {e}")
                return "error"
            blocks = clean([b.to_dict(exclude_none=True) for b in resp.content])
            if resp.stop_reason == "refusal" or all(b["type"] == "fallback" for b in blocks):
                # a refused partial is discarded; an empty assistant message would 400 every later request
                self.placeholder(f"no content: {resp.stop_reason}" if not blocks else "declined")
            else:
                self.add({"role": "assistant", "content": blocks})
            self.track(resp)
            self.ui.end_message()
            if resp.stop_reason == "pause_turn":  # a server tool paused; send it back to continue
                continue
            uses = [b for b in blocks if b["type"] == "tool_use"]
            if uses:
                results, interrupted = self.run_tools(uses, resp.stop_reason)
                self.add_user(results)
                if interrupted:
                    return "interrupted"
                if resp.stop_reason == "tool_use":
                    if self.last_context > COMPACT_AT and self.compact():  # long tool loops must not outgrow the window
                        self.add_user("Context was compacted. Continue the current task from the summary.")
                    continue
            if resp.stop_reason == "refusal":
                self.ui.warn(f"refused: {getattr(resp.stop_details, 'category', None)}")
            return resp.stop_reason

    def run_tools(self, uses, stop="tool_use"):
        results, interrupted = [], False
        for u in uses:
            self.ui.tool_call(u["name"], u["input"])
            if stop != "tool_use":
                out, err = f"Not run: the response ended ({stop}) before this call was complete.", True
            elif interrupted:
                out, err = "Not run: interrupted by the user.", True
            else:
                try:
                    out, err = self.dispatch(u["name"], u["input"]), False
                except KeyboardInterrupt:
                    interrupted, out, err = True, "Interrupted by the user.", True
                except ToolError as e:
                    out, err = str(e), True
                except Exception as e:
                    out, err = f"{type(e).__name__}: {e}", True
            self.ui.tool_result(out, err)
            r = {"type": "tool_result", "tool_use_id": u["id"], "content": truncate(out)}
            results.append({**r, "is_error": True} if err else r)
        return results, interrupted

    def dispatch(self, name, args):
        if name in ("read", "write", "edit", "bash"):
            try:
                return getattr(self.local, name)(**args)
            except TypeError as e:
                raise ToolError(f"Bad arguments: {e}")
        if name == "task":
            return self.run_subagent(args["prompt"], args.get("model"))
        if name == "goal_done":
            if not self.goal or self.goal["status"] != "active":
                return "No goal is active."
            self.goal_claim = args.get("evidence", "")
            return "Recorded. The harness will check it after this turn."
        if name.startswith("mcp__") and self.hub and name in self.hub.tools:
            if not self.approver.ok("mcp", f"{name} {json.dumps(args)[:200]}"):
                raise ToolError("User denied this tool call.")
            out, is_err = self.hub.call(name, args)
            if is_err:
                raise ToolError(out)
            return out
        raise ToolError(f"Tool {name} is not available in this session.")

    # --- subagents --------------------------------------------------------
    def run_subagent(self, prompt, model=None):
        path = self.session.path.with_name(f"{self.session.id}-sub-{secrets.token_hex(3)}.jsonl")
        child = Agent(self.cwd, Session(path), self.ui.child(), self.approver, model=model, effort="medium", sub=True,
                      client=self.client)
        stop = child.turn(prompt)
        self.cost += child.cost
        self.session.append("usage", cost=round(child.cost, 6), subagent=path.name)
        if stop == "interrupted":
            raise KeyboardInterrupt
        if stop in ("error", "refusal"):
            raise ToolError(f"The subagent stopped ({stop}): {child.final_text()}")
        return child.final_text() or f"[subagent stopped ({stop}) without an answer]"

    # --- compaction -------------------------------------------------------
    def compact(self) -> bool:
        """Swap the whole history for one server-written summary block."""
        if not self.messages:
            return False
        try:
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=[{"type": "text", "text": self.system}], tools=self.tools,
                messages=self.messages, compaction={"type": "summarize"}, cache_control={"type": "ephemeral"},
                betas=list(BETAS))
        except Exception as e:
            self.ui.warn(f"compaction failed: {e}")
            return False
        self.track(resp)
        if resp.stop_reason != "compaction":
            self.ui.warn(f"no summary came back ({resp.stop_reason}); keeping the full history")
            return False
        msg = {"role": "assistant", "content": [b.to_dict(exclude_none=True) for b in resp.content]}
        self.messages = [msg]
        self.session.append("compact", message=msg)
        self.last_context = 0
        self.ui.note("compacted")
        if self.goal and self.goal["status"] == "active":
            self.pending_system.append(self.goal_rules())
        return True

    def maybe_compact(self):
        if self.last_context > COMPACT_AT:
            self.ui.note(f"context at {self.last_context:,} tokens, compacting")
            self.compact()

    # --- /goal ------------------------------------------------------------
    def goal_rules(self):
        g = self.goal
        verify = f"After you call it, the harness runs `{g['verify']}` and only accepts the goal if it passes. " if g.get("verify") else ""
        return GOAL_RULES.format(objective=g["objective"], verify=verify)

    def save_goal(self):
        self.session.append("goal", goal=self.goal)

    def set_goal(self, objective, verify=None, max_turns=25, budget=None):
        self.goal = {"objective": objective, "status": "paused", "turns": 0, "max_turns": max_turns,
                     "verify": verify, "budget": budget, "spent": 0.0, "note": ""}
        return self.run_goal(f"Work toward this goal until it is done:\n{objective}")

    def clear_goal(self):
        if self.goal:
            self.goal["status"] = "cleared"
            self.save_goal()
            self.pending_system.append(GOAL_OFF.format(status="cleared by the user"))
            self.goal = None

    def run_goal(self, first="Resume work toward the goal."):
        g = self.goal
        g["status"], g["note"] = "active", ""
        self.save_goal()
        self.pending_system.append(self.goal_rules())  # restated on every start, so a resume after compaction has it
        start_cost, run_start = self.cost - g["spent"], g["spent"]
        limit = g["turns"] + g["max_turns"]  # limits apply per run, so a resume gets a fresh allowance
        message = first
        try:
            while True:
                self.maybe_compact()
                stop = self.turn(message)
                g["turns"] += 1
                g["spent"] = self.cost - start_cost
                if stop in ("interrupted", "error", "refusal"):
                    g["status"], g["note"] = "paused", stop
                    break
                if self.goal_claim is not None:
                    ok, out = self.verify()
                    if ok:
                        g["status"], g["note"] = "done", self.goal_claim[:500]
                        break
                    message = f"The harness ran `{g['verify']}` and it failed:\n{out[-4000:]}\nKeep working."
                elif "BLOCKED:" in self.final_text():
                    g["status"], g["note"] = "paused", "blocked: waiting for the user"
                    break
                else:
                    message = "Continue toward the goal. Call goal_done with evidence when it is fully met."
                if g["turns"] >= limit:
                    g["status"], g["note"] = "paused", f"turn limit ({g['max_turns']} per run)"
                    break
                if g.get("budget") and g["spent"] - run_start >= g["budget"]:
                    g["status"], g["note"] = "paused", f"budget (${g['budget']} per run)"
                    break
        except BaseException:
            g["status"], g["note"] = "paused", "interrupted"
            raise
        finally:
            self.save_goal()
            self.pending_system.append(GOAL_OFF.format(status=g["status"]))
        return g

    def verify(self):
        cmd = self.goal.get("verify")
        if not cmd:
            return True, ""
        self.ui.note(f"verifying: {cmd}")
        try:
            code, out, err = run_shell(cmd, self.cwd, 600)
        except subprocess.TimeoutExpired:
            return False, "The verify command timed out after 600s."
        return code == 0, out + err
