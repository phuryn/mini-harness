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
    _, loaded, _ = Session(agent.session.path).load()
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
    assert (agent.session.path.parent / f"{agent.session.id}-sub1.jsonl").exists()


def test_dropped_thinking_blocks_are_reported(ws):
    drop = [{"type": "thinking_dropped", "path": "messages.1.content.0", "reason": "prefix_binding_mismatch"}]
    agent, _, ui = make(ws, [reply(text("x"), transformations=drop)])
    agent.turn("hi")
    assert any("prefix_binding_mismatch" in w for w in ui.warnings)
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
    ap = Approver(interactive=False, allow=("pytest",))
    assert ap.ok("bash", "ls -la")
    assert ap.ok("bash", "pytest -q")
    assert not ap.ok("bash", "rm -rf build")
    assert not ap.ok("bash", "pytest; rm -rf /")
    assert not ap.ok("write", "/etc/hosts")


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
