"""The agent loop: call the model, run the tools it asks for, repeat.

Three rules keep this loop valid on current Claude models:
1. Append the model's content blocks exactly as returned (thinking signatures,
   compaction blocks). Never append just the text.
2. Never edit what was already sent. System prompt and tools are frozen per
   session; later instructions are appended as role:"system" messages.
3. Every tool_use gets a tool_result in the next user message, even when the
   call was denied, failed or interrupted.
"""
import os
import subprocess

import anthropic

from .context import build_system
from .session import Session
from .tools import SCHEMAS, WEB_SEARCH, LocalTools, ToolError, truncate

MAIN_MODEL = os.environ.get("MINI_MODEL", "claude-opus-5-5")
SUB_MODEL = "claude-sonnet-5-5"
COMPACT_AT = int(os.environ.get("MINI_COMPACT_AT", 150_000))  # input tokens
BETAS = ["compact-2026-09-04", "thinking-binding-controls-2026-08-01"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# $ per million tokens: input, output, cache read, 5-minute cache write.
# Haiku 5.5 cache rates assume the usual 0.1x / 1.25x multipliers.
PRICES = {"claude-opus-5-5": (4, 20, 0.20, 5), "claude-sonnet-5-5": (2, 10, 0.20, 2.5),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
WEB_SEARCH_PRICE = 0.01  # $ per search

# Anthropic's published summarization prompt; used only when /compact gets a focus.
SUMMARY_PROMPT = (
    "Summarize the transcript inside <summary></summary> tags. Include relevant information in the summary such that "
    "this conversation will be continued by a new context window without needing to redo work or be reprovided with "
    "relevant constraints or context. Be sure to preserve: (1) any difficulties or problems that came up, and how they "
    "were handled or resolved; (2) any possibilities, options, or approaches that were raised, tried, or set aside, and "
    "why; (3) anything that was asked for, decided, agreed, ruled out, or established as a preference, constraint, or "
    "boundary - stated exactly; (4) exactly where things stand now - what has been covered, settled, or completed so "
    "far; (5) anything still open, unresolved, promised, or expected to happen next; (6) specific details that would be "
    "hard to reconstruct - names, numbers, dates, exact wording, links or references - kept exactly. Be complete on "
    "these even at the cost of length; keep everything else concise. Weight the two voices differently: keep what the "
    "user said, asked for, shared, or established carefully and close to their own words; your own explanations and "
    "reasoning can be condensed much further, to what they concluded or produced - as long as nothing in the six items "
    "above is dropped. Do not call any tools while writing this summary; respond with text only.")

GOAL_RULES = (
    "Goal mode is on. Objective: {objective}\n"
    "The harness will keep asking you to continue until you call goal_done. Call goal_done only when the objective is "
    "fully met and you have checked it, and put the evidence (commands run, results) in the call. {verify}"
    "If you cannot make progress without the user, end your message with a line that starts with BLOCKED: and the question.")

EXTRA_SCHEMAS = {
    "task": {
        "description": "Delegate a self-contained task to a subagent with a fresh context and read-only tools "
                       "(read, bash, web_search). Returns only its final answer. Give it everything it needs in the prompt.",
        "input_schema": {"type": "object", "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string", "enum": ["claude-sonnet-5-5", "claude-haiku-5-5"],
                      "description": "Default claude-sonnet-5-5; haiku for plain reading or search"}},
            "required": ["prompt"]}},
    "goal_done": {
        "description": "Declare the active goal complete. Only call when it is fully met and checked.",
        "input_schema": {"type": "object", "properties": {"evidence": {"type": "string"}}, "required": ["evidence"]}},
}


def dump(block) -> dict:
    return block.to_dict(exclude_none=True)


class Agent:
    def __init__(self, cwd, session: Session, ui, approver, hub=None, skills=(), model=None, effort="high", sub=False,
                 client=None):
        self.cwd, self.session, self.ui, self.approver, self.hub, self.sub = cwd, session, ui, approver, hub, sub
        self.client = client or anthropic.Anthropic()
        self.local = LocalTools(cwd, approver)
        self.effort, self.cost, self.last_context, self.subs = effort, 0.0, 0, 0
        self.pending_system: list[str] = []
        self.goal, self.goal_claim = None, None
        meta, self.messages, goal = session.load()
        if meta:  # resume: replay the frozen prompt and tools byte for byte
            self.model, self.system, self.tools = meta["model"], meta["system"], meta["tools"]
            self.goal = goal
            self.cost = sum(e.get("cost", 0) for e in session.events() if e["kind"] == "usage")
        else:
            self.model = model or (SUB_MODEL if sub else MAIN_MODEL)
            names = ["read", "bash"] if sub else ["read", "write", "edit", "bash"]
            self.tools = [{"name": n, **SCHEMAS[n]} for n in names]
            if not sub:
                self.tools += [{"name": n, **s} for n, s in EXTRA_SCHEMAS.items()]
                self.tools += hub.schemas if hub else []
            self.tools.append(WEB_SEARCH)
            self.system = build_system(cwd, list(skills), hub.errors if hub else [])
            session.append("meta", model=self.model, cwd=str(cwd), system=self.system, tools=self.tools)

    # --- transcript -------------------------------------------------------
    def add(self, message: dict):
        self.messages.append(message)
        self.session.append("msg", message=message)

    def add_user(self, content):
        self.add({"role": "user", "content": content})
        for text in self.pending_system:  # a system message must follow a user message
            self.add({"role": "system", "content": text})
        self.pending_system = []

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
        pin, pout, pread, pwrite = PRICES.get(resp.model, PRICES[MAIN_MODEL])
        parts = u.iterations or [u]
        cost = sum(((getattr(i, "input_tokens", 0) or 0) * pin + (getattr(i, "output_tokens", 0) or 0) * pout
                    + (getattr(i, "cache_read_input_tokens", 0) or 0) * pread
                    + (getattr(i, "cache_creation_input_tokens", 0) or 0) * pwrite) / 1e6 for i in parts)
        if u.server_tool_use:
            cost += (u.server_tool_use.web_search_requests or 0) * WEB_SEARCH_PRICE
        self.cost += cost
        self.last_context = (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + \
            (u.cache_creation_input_tokens or 0) + (u.output_tokens or 0)
        self.session.append("usage", usage=u.to_dict(exclude_none=True), cost=round(cost, 6))
        drops = [t.to_dict() for t in (resp.input_transformations or [])]
        if drops:  # any entry here means the transcript was edited somewhere
            self.session.append("transform", items=drops)
            self.ui.warn(f"API dropped {len(drops)} thinking block(s): {drops[0].get('reason')}")
        return u

    # --- one user turn ----------------------------------------------------
    def turn(self, content) -> str:
        """Run until the model stops asking for tools. Returns the stop reason."""
        self.goal_claim = None
        self.add_user(content)
        while True:
            try:
                resp = self.call()
            except KeyboardInterrupt:
                self.add({"role": "assistant", "content": [{"type": "text", "text": "(interrupted by the user)"}]})
                self.ui.warn("interrupted")
                return "interrupted"
            blocks = [dump(b) for b in resp.content]
            self.add({"role": "assistant", "content": blocks})
            self.track(resp)
            self.ui.end_message(resp.stop_reason, self)
            if resp.stop_reason == "pause_turn":  # a server tool paused; send it back to continue
                continue
            uses = [b for b in blocks if b["type"] == "tool_use"]
            if uses:
                complete = resp.stop_reason == "tool_use"
                results, interrupted = self.run_tools(uses, complete)
                self.add_user(results)
                if interrupted:
                    return "interrupted"
                if complete:
                    continue
            if resp.stop_reason == "refusal":
                self.ui.warn(f"refused: {getattr(resp.stop_details, 'category', None)}")
            return resp.stop_reason

    def run_tools(self, uses, complete=True):
        results, interrupted = [], False
        for u in uses:
            self.ui.tool_call(u["name"], u["input"])
            if not complete:
                out, err = "Not run: your message hit max_tokens before this call was complete.", True
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
            out, is_err = self.hub.call(name, args)
            if is_err:
                raise ToolError(out)
            return out
        raise ToolError(f"Tool {name} is not available in this session.")

    # --- subagents --------------------------------------------------------
    def run_subagent(self, prompt, model=None):
        self.subs += 1
        path = self.session.path.with_name(f"{self.session.id}-sub{self.subs}.jsonl")
        child = Agent(self.cwd, Session(path), self.ui.child(), self.approver, model=model, effort="medium", sub=True,
                      client=self.client)
        stop = child.turn(prompt)
        self.cost += child.cost
        self.session.append("usage", cost=round(child.cost, 6), subagent=path.name)
        text = child.final_text()
        if stop == "interrupted":
            raise KeyboardInterrupt
        return text or f"[subagent stopped ({stop}) without an answer]"

    # --- compaction -------------------------------------------------------
    def compact(self, focus: str | None = None) -> bool:
        """Swap the whole history for one server-written summary block."""
        if not self.messages:
            return False
        compaction = {"type": "summarize"}
        if focus:  # custom instructions replace the default prompt, so include it
            compaction["instructions"] = SUMMARY_PROMPT + f"\n\nThe user asked you to make sure to keep: {focus}"
        resp = self.client.beta.messages.create(
            model=self.model, max_tokens=16000, system=[{"type": "text", "text": self.system}], tools=self.tools,
            messages=self.messages, compaction=compaction, betas=list(BETAS))
        self.track(resp)
        if resp.stop_reason != "compaction":
            self.ui.warn(f"no summary came back ({resp.stop_reason}); keeping the full history")
            return False
        msg = {"role": "assistant", "content": [dump(b) for b in resp.content]}
        self.messages = [msg]
        self.session.append("compact", message=msg)
        self.last_context = 0
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
        self.goal = {"objective": objective, "status": "active", "turns": 0, "max_turns": max_turns,
                     "verify": verify, "budget": budget, "spent": 0.0, "note": ""}
        self.save_goal()
        self.pending_system.append(self.goal_rules())
        return self.run_goal(f"Work toward this goal until it is done:\n{objective}")

    def run_goal(self, first="Resume work toward the goal."):
        g = self.goal
        g["status"] = "active"
        start_cost = self.cost - g["spent"]
        message = first
        while True:
            self.maybe_compact()
            stop = self.turn(message)
            g["turns"] += 1
            g["spent"] = self.cost - start_cost
            if stop == "interrupted":
                g["status"], g["note"] = "paused", "interrupted"
                break
            if self.goal_claim is not None:
                ok, out = self.verify()
                if ok:
                    g["status"], g["note"] = "done", self.goal_claim[:500]
                    break
                message = f"The harness ran `{g['verify']}` and it failed:\n{truncate(out)[:4000]}\nKeep working."
            elif "BLOCKED:" in self.final_text():
                g["status"], g["note"] = "paused", "blocked: waiting for the user"
                break
            elif stop == "refusal":
                g["status"], g["note"] = "paused", "refused"
                break
            else:
                message = "Continue toward the goal. Call goal_done with evidence when it is fully met."
            if g["turns"] >= g["max_turns"]:
                g["status"], g["note"] = "paused", f"turn limit ({g['max_turns']})"
                break
            if g.get("budget") and g["spent"] >= g["budget"]:
                g["status"], g["note"] = "paused", f"budget (${g['budget']})"
                break
        self.save_goal()
        return g

    def verify(self):
        cmd = self.goal.get("verify")
        if not cmd:
            return True, ""
        self.ui.note(f"verifying: {cmd}")
        r = subprocess.run(cmd, shell=True, cwd=self.cwd, capture_output=True, text=True, timeout=600)
        return r.returncode == 0, (r.stdout + r.stderr)
