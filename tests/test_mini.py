import json
import sys
from pathlib import Path

import pytest

from mini.ext import MCPHub, find_skills
from mini.loop import Agent
from mini.session import Session, list_sessions
from mini.tools import Approver, LocalTools, ToolError

from fake import FakeClient, QuietUI, reply, text, thinking, tool, usage


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr("mini.session.HOME", tmp_path / "home")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def make(ws, script, session=None, **kw):
    client = FakeClient(script)
    ui = QuietUI()
    agent = Agent(str(ws), session or Session.create(str(ws)), ui, Approver(auto=True), client=client, **kw)
    return agent, client, ui


def assert_append_only(requests):
    """Each request must repeat the previous one's system, tools and messages unchanged."""
    for a, b in zip(requests, requests[1:]):
        assert a["system"] == b["system"]
        assert a["tools"] == b["tools"]
        assert b["messages"][:len(a["messages"])] == a["messages"]


def test_tool_loop_is_append_only_and_replays_thinking(ws):
    (ws / "a.txt").write_text("hello\n")
    t = tool("read", {"path": "a.txt"})
    agent, client, _ = make(ws, [reply(thinking("s1"), t, stop="tool_use"), reply(thinking("s2"), text("done"))])
    assert agent.turn("read a.txt") == "end_turn"
    r = client.requests
    assert len(r) == 2
    assert_append_only(r)
    assistant = r[1]["messages"][1]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "s1"}
    result = r[1]["messages"][2]["content"][0]
    assert result["tool_use_id"] == t["id"] and "hello" in result["content"]
    assert "is_error" not in result


def test_request_shape(ws):
    agent, client, _ = make(ws, [reply(text("hi"))])
    agent.turn("hi")
    kw = client.requests[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"]["type"] == "adaptive"
    assert kw["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert kw["output_config"] == {"effort": "high"}
    assert {"compact-2026-09-04", "thinking-binding-controls-2026-08-01"} <= set(kw["betas"])
    assert kw["fallbacks"] == "default"
    assert "tool_choice" not in kw
    names = [t["name"] for t in kw["tools"]]
    assert names[:4] == ["read", "write", "edit", "bash"] and "task" in names and "web_search" in names


def test_parallel_calls_answered_in_one_message_and_errors_kept(ws):
    a, b = tool("edit", {"path": "missing.txt", "old": "x", "new": "y"}), tool("bash", {"command": "echo hi"})
    agent, client, _ = make(ws, [reply(a, b, stop="tool_use"), reply(text("ok"))])
    agent.turn("go")
    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == [a["id"], b["id"]]
    assert results[0]["is_error"] is True
    assert "hi" in results[1]["content"]


def test_max_tokens_with_tool_use_still_gets_results(ws):
    t = tool("bash", {"command": "echo hi"})
    agent, _, _ = make(ws, [reply(t, stop="max_tokens")])
    assert agent.turn("go") == "max_tokens"
    last = agent.messages[-1]
    assert last["role"] == "user" and last["content"][0]["is_error"] is True


def test_interrupt_during_tools_backfills_every_result(ws, monkeypatch):
    a, b = tool("bash", {"command": "sleep 100"}), tool("bash", {"command": "echo later"})
    agent, _, _ = make(ws, [reply(a, b, stop="tool_use")])
    monkeypatch.setattr(agent.local, "bash", lambda **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert agent.turn("go") == "interrupted"
    results = agent.messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == [a["id"], b["id"]]
    assert all(r["is_error"] for r in results)


def test_interrupt_during_stream_keeps_roles_alternating(ws):
    agent, client, _ = make(ws, [KeyboardInterrupt(), reply(text("back"))])
    agent.pending_system.append("note")
    assert agent.turn("first") == "interrupted"
    assert [m["role"] for m in agent.messages] == ["user", "system", "assistant"]
    agent.turn("second")
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "system", "assistant", "user"]


def test_pause_turn_resends(ws):
    agent, client, _ = make(ws, [reply(text("searching"), stop="pause_turn"), reply(text("found"))])
    assert agent.turn("search") == "end_turn"
    assert len(client.requests) == 2
    assert_append_only(client.requests)


def test_resume_replays_frozen_system_and_tools(ws):
    (ws / "AGENTS.md").write_text("Rule one.")
    agent, client, _ = make(ws, [reply(text("one"))])
    agent.turn("hello")
    (ws / "AGENTS.md").write_text("Rule two, edited mid-session.")
    again, client2, _ = make(ws, [reply(text("two"))], session=Session(agent.session.path))
    again.turn("again")
    first, second = client.requests[0], client2.requests[0]
    assert second["system"] == first["system"] and "Rule one." in first["system"][0]["text"]
    assert second["tools"] == first["tools"]
    assert second["messages"][:2] == agent.messages


def test_only_agents_md_is_loaded(ws):
    (ws / "AGENTS.md").write_text("from agents")
    (ws / "CLAUDE.md").write_text("from claude")
    agent, _, _ = make(ws, [])
    assert "from agents" in agent.system and "from claude" not in agent.system


def test_compaction_swaps_history_for_the_block(ws):
    block = {"type": "compaction", "content": "Summary: user said hello.", "signature": "c1"}
    agent, client, _ = make(ws, [reply(text("one")), reply(block, stop="compaction"), reply(text("two"))])
    agent.turn("hello")
    assert agent.compact() is True
    assert client.beta.messages.creates[0]["compaction"] == {"type": "summarize"}
    assert "context_management" not in client.beta.messages.creates[0]
    agent.turn("next")
    msgs = client.requests[-1]["messages"]
    assert msgs[0] == {"role": "assistant", "content": [block]}
    assert msgs[1] == {"role": "user", "content": "next"}
    _, loaded, _, _, _ = Session(agent.session.path).load()
    assert loaded == agent.messages
    assert client.requests[-1]["system"] == client.requests[0]["system"]


def test_compaction_failure_keeps_history(ws):
    agent, _, ui = make(ws, [reply(text("one")), reply(stop="max_tokens")])
    agent.turn("hello")
    before = list(agent.messages)
    assert agent.compact() is False
    assert agent.messages == before and ui.warnings


def test_auto_compaction_triggers_on_threshold(ws, monkeypatch):
    monkeypatch.setattr("mini.loop.COMPACT_AT", 500)
    block = {"type": "compaction", "content": "S", "signature": "c"}
    agent, client, _ = make(ws, [reply(text("big"), u=usage(inp=900)), reply(block, stop="compaction")])
    agent.turn("x")
    agent.maybe_compact()
    assert agent.messages[0]["content"][0]["type"] == "compaction"


def test_goal_loop_verifies_before_accepting(ws):
    done = ws / "done.txt"
    script = [
        reply(tool("goal_done", {"evidence": "trust me"}), stop="tool_use"), reply(text("claimed")),
        reply(tool("write", {"path": "done.txt", "content": "x"}), stop="tool_use"),
        reply(tool("goal_done", {"evidence": "wrote done.txt"}), stop="tool_use"), reply(text("now done")),
    ]
    agent, client, _ = make(ws, script)
    g = agent.set_goal("create done.txt", verify="test -f done.txt")
    assert g["status"] == "done" and g["turns"] == 2 and done.exists()
    first = client.requests[0]["messages"]
    assert first[0]["role"] == "user" and first[1]["role"] == "system" and "Goal mode is on" in first[1]["content"]
    retry = [m for m in client.requests[2]["messages"] if m["role"] == "user" and isinstance(m["content"], str)]
    assert "failed" in retry[-1]["content"]
    assert_append_only(client.requests)


def test_goal_stops_when_blocked_and_on_turn_limit(ws):
    agent, _, _ = make(ws, [reply(text("Need input.\nBLOCKED: which file?"))])
    assert agent.set_goal("do it")["status"] == "paused"
    agent2, _, _ = make(ws, [reply(text("working")), reply(text("still working"))])
    g = agent2.set_goal("do it", max_turns=2)
    assert g["status"] == "paused" and "turn limit" in g["note"]


def test_goal_state_survives_resume(ws):
    agent, _, _ = make(ws, [reply(text("BLOCKED: ?"))])
    agent.set_goal("ship it")
    again, _, _ = make(ws, [], session=Session(agent.session.path))
    assert again.goal["objective"] == "ship it" and again.goal["status"] == "paused"


def test_subagent_gets_fresh_context_and_returns_only_text(ws):
    t = tool("task", {"prompt": "count files", "model": "claude-haiku-5-5"})
    script = [reply(t, stop="tool_use"),
              reply(tool("bash", {"command": "ls"}), stop="tool_use", model="claude-haiku-5-5"),
              reply(text("3 files"), model="claude-haiku-5-5"),
              reply(text("the subagent says 3"))]
    agent, client, _ = make(ws, script)
    agent.turn("how many files?")
    child_first = client.requests[1]
    assert child_first["model"] == "claude-haiku-5-5" and child_first["messages"] == [
        {"role": "user", "content": "count files"}]
    assert [x["name"] for x in child_first["tools"]] == ["read", "bash", "web_search"]
    assert "fallbacks" not in child_first
    parent_last = client.requests[3]
    assert parent_last["messages"][-1]["content"][0]["content"] == "3 files"
    assert_append_only([client.requests[0], client.requests[3]])
    assert len(list(agent.session.path.parent.glob(f"{agent.session.id}-sub-*.jsonl"))) == 1


def test_dropped_thinking_blocks_are_reported(ws):
    drop = [{"type": "thinking_dropped", "path": "messages.1.content.0", "reason": "prefix_binding_mismatch"}]
    agent, _, ui = make(ws, [reply(text("x"), transformations=drop)])
    agent.turn("hi")
    assert any("history was edited" in w for w in ui.warnings)
    kinds = [e["kind"] for e in agent.session.events()]
    assert "transform" in kinds


def test_cost_tracking(ws):
    agent, _, _ = make(ws, [reply(text("x"), u=usage(inp=1_000_000, out=100_000, read=1_000_000))])
    agent.turn("hi")
    assert agent.cost == pytest.approx(4 + 2 + 0.2)


def test_session_listing(ws):
    agent, _, _ = make(ws, [reply(text("x"))])
    agent.turn("first prompt")
    rows = list_sessions(str(ws))
    assert rows[0][0] == agent.session.id and rows[0][1] == 1 and rows[0][3] == "first prompt"
    assert Session.find(str(ws)).id == agent.session.id


# --- tools ---------------------------------------------------------------

def test_edit_requires_fresh_read(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    f = ws / "f.txt"
    f.write_text("one two")
    with pytest.raises(ToolError, match="Read the file"):
        tools.edit("f.txt", "one", "1")
    tools.read("f.txt")
    import os
    os.utime(f, (1, 1))
    with pytest.raises(ToolError, match="changed"):
        tools.edit("f.txt", "one", "1")
    tools.read("f.txt")
    tools.edit("f.txt", "one", "1")
    assert f.read_text() == "1 two"


def test_edit_rejects_ambiguous_match(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    (ws / "f.txt").write_text("a a")
    tools.read("f.txt")
    with pytest.raises(ToolError, match="2 times"):
        tools.edit("f.txt", "a", "b")
    tools.edit("f.txt", "a", "b", replace_all=True)
    assert (ws / "f.txt").read_text() == "b b"


def test_write_will_not_clobber_unread_file(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    (ws / "f.txt").write_text("keep")
    with pytest.raises(ToolError, match="Read it first"):
        tools.write("f.txt", "gone")


def test_headless_approver_denies_unlisted_commands(ws):
    ap = Approver(interactive=False, allow=("python3 -m pytest",))
    assert ap.ok("bash", "ls -la")
    assert ap.ok("bash", "python3 -m pytest -q")
    for bad in ["rm -rf build", "python3 -m pytest; rm -rf /", "ls\nrm -rf x", "find . -delete",
                "rg --pre /bin/sh x", "git diff --output=x", "git status", "lsof", "python3 -c 1",
                "cat $(whoami)", "echo hi > f"]:
        assert not ap.ok("bash", bad), bad
    assert not ap.ok("write", "/etc/hosts")


def test_writes_into_dot_git_need_approval(ws):
    (ws / ".git").mkdir()
    tools = LocalTools(str(ws), Approver(interactive=False))
    with pytest.raises(ToolError, match="denied"):
        tools.write(".git/config", "[core]\n fsmonitor = evil")
    assert tools.write("ok.txt", "fine")


def test_write_needs_current_version_and_edit_needs_old(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    f = ws / "f.txt"
    f.write_text("v1")
    tools.read("f.txt")
    import os
    os.utime(f, (1, 1))
    with pytest.raises(ToolError, match="current version"):
        tools.write("f.txt", "v2")
    tools.read("f.txt")
    with pytest.raises(ToolError, match="empty"):
        tools.edit("f.txt", "", "x", replace_all=True)


def test_bash_reports_exit_code(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    assert tools.bash("echo out; exit 3").endswith("[exit 3]")


# --- extensions ------------------------------------------------------------

def test_skills_are_listed_in_system_prompt(ws):
    d = ws / "skills" / "release-notes"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: release-notes\ndescription: Write release notes from git log\n---\nSteps...")
    skills = find_skills(str(ws))
    assert skills == [{"name": "release-notes", "description": "Write release notes from git log",
                       "path": str(d / "SKILL.md")}]
    agent, _, _ = make(ws, [], skills=skills)
    assert "release-notes: Write release notes from git log" in agent.system


def test_mcp_stdio_server_round_trip(ws):
    server = Path(__file__).with_name("echo_server.py")
    (ws / "mcp.json").write_text(json.dumps({"mcpServers": {"echo": {"command": sys.executable, "args": [str(server)]}}}))
    hub = MCPHub(str(ws))
    try:
        assert hub.errors == []
        assert "mcp__echo__shout" in hub.tools
        out, err = hub.call("mcp__echo__shout", {"text": "hi"})
        assert out == "HI" and not err
    finally:
        hub.close()


def test_broken_mcp_server_does_not_stop_startup(ws):
    (ws / "mcp.json").write_text(json.dumps({"mcpServers": {"nope": {"command": "/does/not/exist"}}}))
    hub = MCPHub(str(ws))
    hub.close()
    assert hub.errors and hub.errors[0].startswith("nope")


# --- review round 1 regressions -----------------------------------------------

def test_api_error_keeps_session_valid(ws):
    agent, client, ui = make(ws, [RuntimeError("529 overloaded"), reply(text("ok"))])
    agent.pending_system.append("Goal mode is on.")
    assert agent.turn("first") == "error"
    assert [m["role"] for m in agent.messages] == ["user", "system", "assistant"]
    agent.turn("second")
    roles = [m["role"] for m in client.requests[-1]["messages"]]
    assert roles == ["user", "system", "assistant", "user"]


def test_resume_after_crash_mid_tool_closes_the_tail(ws):
    s = Session.create(str(ws))
    agent, _, _ = make(ws, [reply(tool("bash", {"command": "sleep 1"}), stop="tool_use")], session=s)
    agent.add_user("go")
    agent.add({"role": "assistant", "content": [tool("bash", {"command": "sleep 9"}, tid="toolu_dead")]})
    again, client, _ = make(ws, [reply(text("hi"))], session=Session(s.path))
    again.turn("continue")
    msgs = client.requests[0]["messages"]
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_dead" and msgs[2]["content"][0]["is_error"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]


def test_fallback_partial_is_not_run_or_echoed(ws, monkeypatch):
    declined, kept = tool("bash", {"command": "echo declined"}), tool("bash", {"command": "echo kept"})
    fb = {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
          "trigger": {"type": "refusal", "category": "cyber"}}
    first = reply(thinking("x"), text("partial"), declined, fb, text("again"), kept, stop="tool_use",
                  model="claude-opus-5")
    agent, client, _ = make(ws, [first, reply(text("done"))])
    ran = []
    monkeypatch.setattr(agent.local, "bash", lambda command, **k: ran.append(command) or "ok")
    agent.turn("go")
    assert ran == ["echo kept"]
    echoed = client.requests[1]["messages"][1]["content"]
    assert [b["type"] for b in echoed] == ["text", "text", "tool_use"]


def test_goal_rules_restated_on_resume_after_compaction(ws):
    block = {"type": "compaction", "content": "S", "signature": "c"}
    agent, client, _ = make(ws, [reply(text("BLOCKED: which?")), reply(block, stop="compaction"),
                                 reply(tool("goal_done", {"evidence": "e"}), stop="tool_use"), reply(text("done"))])
    agent.set_goal("ship it")
    agent.compact()
    assert agent.run_goal()["status"] == "done"
    sys_msgs = [m for m in client.requests[-1]["messages"] if m["role"] == "system"]
    assert sys_msgs and "ship it" in sys_msgs[-1]["content"]


def test_cleared_goal_is_not_restored(ws):
    agent, _, _ = make(ws, [reply(text("BLOCKED: ?"))])
    agent.set_goal("x")
    agent.clear_goal()
    again, _, _ = make(ws, [], session=Session(agent.session.path))
    assert again.goal is None


def test_each_iteration_priced_at_its_own_model(ws):
    u = usage(inp=0, out=0)
    u["iterations"] = [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        {"type": "fallback_message", "model": "claude-opus-5", "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]
    agent, _, _ = make(ws, [reply(text("x"), u=u, model="claude-opus-5")])
    agent.turn("hi")
    assert agent.cost == pytest.approx(4 + 5)
    assert agent.last_context == 1_000_000


# --- review round 2 regressions -----------------------------------------------

def test_empty_reply_becomes_a_placeholder(ws):
    agent, client, _ = make(ws, [reply(stop="refusal"), reply(text("ok"))])
    agent.turn("x")
    assert agent.messages[-1]["content"] == [{"type": "text", "text": "(no content: refusal)"}]
    agent.turn("y")
    assert all(m["content"] for m in client.requests[-1]["messages"])


@pytest.mark.parametrize("path", [".GIT/hooks/pre-commit", ".git/config", "mcp.json", "conftest.py",
                                  "sub/.husky/pre-push", "Makefile"])
def test_files_that_run_later_need_approval(ws, path):
    tools = LocalTools(str(ws), Approver(interactive=False))
    with pytest.raises(ToolError, match="denied"):
        tools.write(path, "x")


def test_mcp_calls_need_approval(ws):
    class Hub:
        tools, schemas, errors = {"mcp__mail__send": None}, [], []
        def call(self, name, args): return "sent", False
    agent = Agent(str(ws), Session.create(str(ws)), QuietUI(), Approver(interactive=False), hub=Hub(),
                  client=FakeClient([]))
    with pytest.raises(ToolError, match="denied"):
        agent.dispatch("mcp__mail__send", {"to": "x"})
    agent.approver = Approver(interactive=False, allow=("mcp__mail__",))
    assert agent.dispatch("mcp__mail__send", {"to": "x"}) == "sent"


def test_pending_system_messages_join_into_one(ws):
    agent, client, _ = make(ws, [reply(text("BLOCKED: ?")), reply(text("BLOCKED: again"))])
    agent.set_goal("first")
    agent.clear_goal()
    agent.set_goal("second")
    roles = [m["role"] for m in client.requests[-1]["messages"]]
    assert "system" not in [b for a, b in zip(roles, roles[1:]) if a == "system"]


def test_goal_mode_is_switched_off_when_it_stops(ws):
    agent, client, _ = make(ws, [reply(text("BLOCKED: ?")), reply(text("hi"))])
    agent.set_goal("x")
    agent.turn("unrelated")
    last_sys = [m for m in client.requests[-1]["messages"] if m["role"] == "system"][-1]
    assert "Goal mode is off" in last_sys["content"]


def test_resumed_parent_starts_a_fresh_subagent(ws):
    t1, t2 = tool("task", {"prompt": "a"}), tool("task", {"prompt": "b", "model": "claude-haiku-5-5"})
    agent, _, _ = make(ws, [reply(t1, stop="tool_use"), reply(text("A"), model="claude-sonnet-5-5"), reply(text("ok"))])
    agent.turn("one")
    again, client, _ = make(ws, [reply(t2, stop="tool_use"), reply(text("B"), model="claude-haiku-5-5"),
                                 reply(text("ok"))], session=Session(agent.session.path))
    again.turn("two")
    child = client.requests[1]
    assert child["model"] == "claude-haiku-5-5" and child["messages"] == [{"role": "user", "content": "b"}]


def test_failed_subagent_is_an_error_result(ws):
    agent, client, _ = make(ws, [reply(tool("task", {"prompt": "a"}), stop="tool_use"), RuntimeError("down"),
                                 reply(text("ok"))])
    agent.turn("go")
    assert client.requests[-1]["messages"][-1]["content"][0]["is_error"] is True


def test_bash_timeout_kills_the_process_group(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    with pytest.raises(ToolError, match="Timed out"):
        tools.bash("sleep 30 & sleep 30", timeout=1)


# --- review round 3 regressions -----------------------------------------------

def test_goal_off_notice_survives_restart(ws):
    agent, _, _ = make(ws, [reply(text("BLOCKED: ?"))])
    agent.set_goal("ship it")
    again, client, _ = make(ws, [reply(text("hi"))], session=Session(agent.session.path))
    again.turn("unrelated")
    last_sys = [m for m in client.requests[-1]["messages"] if m["role"] == "system"][-1]
    assert "Goal mode is off" in last_sys["content"]


def test_compaction_inside_a_long_tool_loop(ws, monkeypatch):
    monkeypatch.setattr("mini.loop.COMPACT_AT", 500)
    block = {"type": "compaction", "content": "S", "signature": "c"}
    agent, client, _ = make(ws, [reply(tool("bash", {"command": "echo a"}), stop="tool_use", u=usage(inp=900)),
                                 reply(block, stop="compaction"), reply(text("done"))])
    agent.turn("go")
    msgs = client.requests[-1]["messages"]
    assert msgs[0]["content"][0]["type"] == "compaction" and msgs[-1]["role"] == "user"
    assert "compacted" in msgs[-1]["content"]


def test_resume_restores_context_size(ws):
    agent, _, _ = make(ws, [reply(text("x"), u=usage(inp=200_000))])
    agent.turn("hi")
    again, _, _ = make(ws, [], session=Session(agent.session.path))
    assert again.last_context == agent.last_context > 200_000


@pytest.mark.parametrize("path", ["docs/HEAD", "docs/config"])
def test_git_repo_lookalike_files_need_approval(ws, path):
    with pytest.raises(ToolError, match="denied"):
        LocalTools(str(ws), Approver(interactive=False)).write(path, "x")


def test_every_write_asks_when_started_in_home(ws, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: ws))
    with pytest.raises(ToolError, match="denied"):
        LocalTools(str(ws), Approver(interactive=False)).write("notes.txt", "x")


def test_unknown_model_does_not_break_tracking(ws):
    agent, _, _ = make(ws, [reply(text("x"), model="claude-fable-5-1")])
    assert agent.turn("hi") == "end_turn" and agent.cost > 0


def test_refused_partial_is_discarded(ws):
    agent, _, _ = make(ws, [reply(text("half an answer"), stop="refusal")])
    agent.turn("x")
    assert agent.messages[-1]["content"] == [{"type": "text", "text": "(declined)"}]


def test_truncated_session_line_is_skipped(ws):
    agent, _, _ = make(ws, [reply(text("x"))])
    agent.turn("hi")
    with agent.session.path.open("a") as f:
        f.write('{"kind": "msg", "message": {"ro')
    again, _, _ = make(ws, [], session=Session(agent.session.path))
    assert len(again.messages) == 2 and list_sessions(str(ws))


def test_malformed_mcp_json_is_reported(ws):
    (ws / "mcp.json").write_text("{not json")
    hub = MCPHub(str(ws))
    assert hub.errors and "not a valid MCP config" in hub.errors[0]


def test_empty_allow_entry_approves_nothing(ws):
    ap = Approver(interactive=False, allow=("",))
    assert not ap.ok("bash", "rm -rf x") and not ap.ok("mcp", "mcp__x__y {}")


# --- review round 4 regressions -----------------------------------------------

def test_refusal_with_tool_call_leaves_no_orphan_result(ws):
    agent, client, _ = make(ws, [reply(tool("bash", {"command": "echo x"}), stop="refusal"), reply(text("ok"))])
    agent.turn("x")
    agent.turn("y")
    msgs = client.requests[-1]["messages"]
    assert not any(isinstance(m["content"], list) and m["content"] and m["content"][0].get("type") == "tool_result"
                   for m in msgs)


def test_append_after_cut_off_line_is_not_lost(ws):
    s = Session.create(str(ws))
    s.append("msg", message={"role": "user", "content": "a"})
    with s.path.open("a") as f:
        f.write('{"kind": "msg", "mess')
    s.append("msg", message={"role": "assistant", "content": [{"type": "text", "text": "b"}]})
    assert [m["role"] for m in s.load()[1]] == ["user", "assistant"]


def test_goal_left_active_by_a_killed_process_is_paused_on_resume(ws):
    agent, _, _ = make(ws, [])
    agent.goal = {"objective": "x", "status": "active", "turns": 1, "max_turns": 5, "verify": None,
                  "budget": None, "spent": 0.0, "note": ""}
    agent.save_goal()
    agent.add_user("go")
    agent.add({"role": "system", "content": "Goal mode is on. Objective: x"})
    again, client, _ = make(ws, [reply(text("hi"))], session=Session(agent.session.path))
    assert again.goal["status"] == "paused"
    again.turn("chat")
    assert "Goal mode is off" in [m for m in client.requests[-1]["messages"] if m["role"] == "system"][-1]["content"]


def test_approval_prompt_shows_everything_escaped(ws, monkeypatch):
    shown = []
    monkeypatch.setattr("builtins.input", lambda p: shown.append(p) or "n")
    ap = Approver()
    ap.ok("bash", "echo " + "a" * 300 + "\r; rm -rf ~")
    assert "rm -rf ~" in shown[0] and "\\r" in shown[0] and "\r" not in shown[0]
    assert not ap.ok("bash", "x" * 5000)


def test_gnumakefile_is_protected(ws):
    with pytest.raises(ToolError, match="denied"):
        LocalTools(str(ws), Approver(interactive=False)).write("GNUmakefile", "x")


def test_failed_compaction_backs_off(ws, monkeypatch):
    monkeypatch.setattr("mini.loop.COMPACT_AT", 500)
    agent, client, _ = make(ws, [reply(text("x"), u=usage(inp=1000)), reply(stop="max_tokens")])
    agent.turn("hi")
    agent.maybe_compact()
    agent.maybe_compact()
    assert len(client.beta.messages.creates) == 1


# --- review round 5 regressions -----------------------------------------------

def compaction_usage(stop_reason):
    u = usage(inp=0, out=0)
    u["iterations"] = [{"type": "compaction", "input_tokens": 200_000, "output_tokens": 10,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]
    return reply(stop=stop_reason, u=u)


def test_compaction_backoff_with_real_usage_shape(ws, monkeypatch):
    monkeypatch.setattr("mini.loop.COMPACT_AT", 500)
    agent, client, _ = make(ws, [reply(text("x"), u=usage(inp=1000)), compaction_usage("refusal"),
                                 reply(text("y"), u=usage(inp=1100))])
    agent.turn("hi")
    agent.maybe_compact()
    assert agent.last_context > 500 and agent.compact_failed_at > 500
    agent.turn("again")
    agent.maybe_compact()
    assert len(client.beta.messages.creates) == 1


def test_paused_server_tool_is_not_committed_if_the_continuation_fails(ws):
    paused = reply(text("searching"), {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                                       "input": {"query": "x"}}, stop="pause_turn")
    agent, client, _ = make(ws, [paused, RuntimeError("529"), reply(text("ok"))])
    assert agent.turn("search") == "error"
    assert not any(b.get("type") == "server_tool_use" for m in agent.messages if m["role"] == "assistant"
                   for b in m["content"])
    assert client.requests[1]["messages"][-1]["content"][1]["type"] == "server_tool_use"  # sent back unchanged


def test_binary_output_is_decoded_not_crashed(ws):
    (ws / "b.bin").write_bytes(b"\xff\xfe\x00abc")
    out = LocalTools(str(ws), Approver(auto=True)).bash("cat b.bin")
    assert "abc" in out


def test_wrongly_shaped_mcp_json_is_reported(ws):
    (ws / "mcp.json").write_text('{"mcpServers": []}')
    assert "must be an object" in MCPHub(str(ws)).errors[0]


def test_protected_write_prompt_shows_the_content(ws, monkeypatch):
    shown = []
    monkeypatch.setattr("builtins.input", lambda p: shown.append(p) or "n")
    with pytest.raises(ToolError):
        LocalTools(str(ws), Approver()).write("conftest.py", "import os; os.system('evil')")
    assert "os.system" in shown[0]


# --- tool definitions and review round 6 --------------------------------------

def test_local_tools_are_strict_and_explain_their_pitfalls(ws):
    agent, _, _ = make(ws, [])
    tools = {t["name"]: t for t in agent.tools}
    for name in ["read", "write", "edit", "bash", "task", "goal_done"]:
        assert tools[name]["strict"] is True
        assert tools[name]["input_schema"]["additionalProperties"] is False
        assert set(tools[name]["input_schema"]["required"]) <= set(tools[name]["input_schema"]["properties"])
    assert "line-number" in tools["edit"]["description"]
    assert "can't see this conversation" in tools["task"]["description"]
    assert "no input" in tools["bash"]["description"]


def test_paused_and_final_reply_are_one_session_line(ws):
    paused = reply({"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "x"}},
                   stop="pause_turn")
    done = reply({"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []}, text("found"))
    agent, _, _ = make(ws, [paused, done])
    agent.turn("search")
    kinds = [e["kind"] for e in agent.session.events()]
    assert kinds.count("msgs") == 1
    assert Session(agent.session.path).load()[1] == agent.messages


def test_goal_rules_win_over_an_earlier_off_notice(ws):
    agent, client, _ = make(ws, [reply(text("hi"))])
    agent.goal = {"objective": "x", "status": "paused", "turns": 0, "max_turns": 5, "verify": None,
                  "budget": None, "spent": 0.0, "note": ""}
    agent.pending_system = [agent.goal_rules(), "Goal mode is off (status: paused).", agent.goal_rules()]
    agent.turn("go")
    content = client.requests[0]["messages"][1]["content"]
    assert content.rstrip().endswith(agent.goal_rules().rstrip())


def test_repl_goal_uses_command_line_limits(ws):
    from mini.cli import command
    agent, _, _ = make(ws, [reply(tool("goal_done", {"evidence": "e"}), stop="tool_use"), reply(text("done")),
                            reply(text("BLOCKED: tests fail"))])
    agent.goal_defaults = {"verify": "false", "max_turns": 7, "budget": None}
    command(agent, "/goal make it pass", lambda s: agent)
    assert agent.goal["verify"] == "false" and agent.goal["status"] == "paused"


def test_edit_keeps_windows_line_endings(ws):
    f = ws / "w.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    tools = LocalTools(str(ws), Approver(auto=True))
    tools.read("w.txt")
    tools.edit("w.txt", "a\nb", "a\nB")
    assert f.read_bytes() == b"a\r\nB\r\nc\r\n"


def test_overwriting_an_executable_needs_approval(ws):
    f = ws / "run.sh"
    f.write_text("echo hi")
    f.chmod(0o755)
    tools = LocalTools(str(ws), Approver(interactive=False))
    tools.read("run.sh")
    with pytest.raises(ToolError, match="denied"):
        tools.write("run.sh", "rm -rf ~")


def test_read_refuses_huge_and_special_files(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    with pytest.raises(ToolError, match="regular file"):
        tools.read("/dev/zero")


# --- review round 7 -----------------------------------------------------------

def test_read_pages_before_the_output_cap(ws):
    (ws / "big.py").write_text("\n".join(f"x_{i} = '{'a' * 70}'" for i in range(600)) + "\n")
    tools = LocalTools(str(ws), Approver(auto=True))
    first = tools.read("big.py")
    assert len(first) < 30_000 and "[... " not in first
    nxt = int(first.rsplit("offset=", 1)[1].rstrip("]"))
    second = tools.read("big.py", offset=nxt)
    assert second.splitlines()[0].strip().startswith(f"{nxt}\t")
    assert tools.read("big.py", limit=-5).count("\n") >= 1


def test_crlf_edge_cases(ws):
    tools = LocalTools(str(ws), Approver(auto=True))
    f = ws / "m.txt"
    f.write_bytes(b"header\r\nb\nc\n")
    tools.read("m.txt")
    tools.edit("m.txt", "b\nc", "B\nC")
    assert f.read_bytes() == b"header\r\nB\nC\n"
    g = ws / "w.txt"
    g.write_bytes(b"a\r\nb\r\n")
    tools.read("w.txt")
    tools.edit("w.txt", "a\nb", "x\r\ny")
    assert g.read_bytes() == b"x\r\ny\r\n"
    tools.read("w.txt")
    tools.write("w.txt", "one\ntwo\n")
    assert g.read_bytes() == b"one\r\ntwo\r\n"


def test_negative_timeout_is_clamped(ws):
    assert LocalTools(str(ws), Approver(auto=True)).bash("echo ok", timeout=-1) == "ok"


def test_status_line_after_compaction_does_not_claim_zero_tokens(ws):
    from mini.cli import status_line
    agent, _, _ = make(ws, [])
    agent.last_context = 0
    assert "0 tokens" not in status_line(agent)
    agent.last_context = 1234
    assert "1,234 tokens in context" in status_line(agent)
