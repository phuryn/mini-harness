# AGENTS.md

mini is a minimal agent harness for Claude: a tool loop, five local tools, append-only sessions, server-side compaction, skills, MCP over stdio, subagents and `/goal`. It exists to be read. Keep it small.

## Rules for changing it

- **The transcript is append-only.** Never edit, reorder or delete a message once it is in `Agent.messages` or the session file. The system prompt and tool list are frozen when a session starts and replayed from the session's `meta` event on resume. Anything that changes mid-session is appended as a `{"role": "system"}` message (via `Agent.pending_system`). Claude rejects replayed thinking blocks after a history edit, so a violation here is a real bug.
- **Every `tool_use` gets a `tool_result`** in the very next user message, in one message for parallel calls, with `is_error: true` for failures, denials and interrupts.
- **Append the model's blocks as returned** (`block.to_dict(exclude_none=True)`), never just the text. The one exception is `clean()` in `loop.py`, which drops a declined model's unpaired calls and thinking from before a `fallback` block, as Anthropic's fallback guide requires.
- **Size budget:** `src/mini/` stays around 1,000 lines of code (excluding blanks, comments and docstrings; 999 with DeepSeek, the judge and `ask_user` after thirteen review rounds, 1,075 with `find`, the goal reviewer, ask_user options, `/dream` and Agent Skills headers, which Pawel approved past the line, 1,100 with the Windows port (Git Bash, no execute bit, UTF-8 file I/O, `MINI_NO_WEB`), 1,098 with models and prices moved to `models.json`, 1,134 with `/context` (measured split), Windows colors and quieter output, 1,166 with the read gate (outside the project and secrets) and Jev as the default judge, 1,202 with forgiving edits (line endings, trailing whitespace, closest-line hint), read-only git and clearer denials, 1,303 with images in `read`, approval text that matches the permissions, recovery from the output cap, background jobs, bash on Linux and web search without the code sandbox (a tokenize count that puts 1.0.1 at 1,210); about 180 are prompt and tool-description text). Safety fixes come before the budget. A feature that pushes past the budget is probably out of scope, so say so instead of adding it.
- **No new dependencies** beyond `anthropic` and `mcp`.
- Plain Python 3.12, standard library first. Comments explain why, not what.

## Layout

| File | What it owns |
|---|---|
| `src/mini/loop.py` | `Agent`: model calls, tool dispatch, subagents, compaction, `/goal`, the judge, providers, cost |
| `src/mini/tools.py` | read, write, edit, bash, find, and the approval gate |
| `src/mini/session.py` | JSONL session files, resume, listing |
| `src/mini/context.py` | the frozen system prompt (base prompt, AGENTS.md, memory index, skills) |
| `src/mini/models.json` | default models, prices, context windows, compaction thresholds, other providers |
| `src/mini/ext.py` | skill discovery and the MCP stdio hub |
| `src/mini/cli.py` | REPL, slash commands, headless mode |
| `tests/` | offline tests against a scripted fake client, plus a real MCP stdio server |

## Checks

```
uv run pytest -q
```

Live smoke test (needs `ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY` with `--model deepseek-flash`; spends a few cents):

```
cd /some/scratch/dir && uv run --project ~/mini mini --model claude-haiku-5-5 -p "list the files here"
```

Tests must pass before every commit. A change to the loop needs a test that asserts the append-only invariant (`assert_append_only` in `tests/test_mini.py`).
