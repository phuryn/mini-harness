# AGENTS.md

mini is a minimal agent harness for Claude: a tool loop, four local tools, append-only sessions, server-side compaction, skills, MCP over stdio, subagents and `/goal`. It exists to be read. Keep it small.

## Rules for changing it

- **The transcript is append-only.** Never edit, reorder or delete a message once it is in `Agent.messages` or the session file. The system prompt and tool list are frozen when a session starts and replayed from the session's `meta` event on resume. Anything that changes mid-session is appended as a `{"role": "system"}` message (via `Agent.pending_system`). Claude rejects replayed thinking blocks after a history edit, so a violation here is a real bug.
- **Every `tool_use` gets a `tool_result`** in the very next user message, in one message for parallel calls, with `is_error: true` for failures, denials and interrupts.
- **Append the model's blocks as returned** (`block.to_dict(exclude_none=True)`), never just the text. The one exception is `clean()` in `loop.py`, which drops a declined model's unpaired calls and thinking from before a `fallback` block, as Anthropic's fallback guide requires.
- **Size budget:** `src/mini/` stays under about 800 lines of code (excluding blanks, comments and docstrings; 821 after three review rounds). Safety fixes come before the budget. A feature that pushes past the budget is probably out of scope, so say so instead of adding it.
- **No new dependencies** beyond `anthropic` and `mcp`.
- Plain Python 3.12, standard library first. Comments explain why, not what.

## Layout

| File | What it owns |
|---|---|
| `src/mini/loop.py` | `Agent`: model calls, tool dispatch, subagents, compaction, `/goal`, cost |
| `src/mini/tools.py` | read, write, edit, bash, and the approval gate |
| `src/mini/session.py` | JSONL session files, resume, listing |
| `src/mini/context.py` | the frozen system prompt (base prompt, AGENTS.md, memory index, skills) |
| `src/mini/ext.py` | skill discovery and the MCP stdio hub |
| `src/mini/cli.py` | REPL, slash commands, headless mode |
| `tests/` | offline tests against a scripted fake client, plus a real MCP stdio server |

## Checks

```
uv run pytest -q
```

Live smoke test (needs `ANTHROPIC_API_KEY`; spends a few cents):

```
cd /some/scratch/dir && uv run --project ~/mini mini --model claude-haiku-5-5 -p "list the files here"
```

Tests must pass before every commit. A change to the loop needs a test that asserts the append-only invariant (`assert_append_only` in `tests/test_mini.py`).
