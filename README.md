# mini

A minimal agent harness for Claude, small enough to read in one sitting. It has a tool loop, four local tools, sessions you can resume, compaction, skills, MCP servers, subagents, a `/goal` mode, and memory kept in markdown files. It has no UI framework, no plugin system and no dependencies beyond `anthropic` and `mcp`.

It's built to show what a harness actually has to do, and which parts you can leave out.

## Quick start

```bash
git clone <this repo> ~/mini && cd ~/mini && uv sync
export ANTHROPIC_API_KEY=sk-ant-...
cd ~/some/project
uv run --project ~/mini mini                 # interactive
uv run --project ~/mini mini -p "explain this repo in five bullets"
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Talking to it

| Command | What it does |
|---|---|
| `/new` | Start a fresh session |
| `/sessions` | List recent sessions in this directory |
| `/resume [id]` | Resume the latest session, or one by id prefix |
| `/compact [focus]` | Replace the history with a summary now; optionally say what it must keep |
| `/goal <objective>` | Keep working until the objective is done; also `pause`, `resume`, `clear`, `status` |
| `/exit` | Quit. Every message is already saved |

Ctrl-C stops the current turn and keeps the session valid.

| Flag | What it does |
|---|---|
| `-p "..."` | Run one prompt headless and print the answer |
| `-c` / `-r <id>` | Continue the latest session, or resume one |
| `--goal "..." --verify "pytest -q"` | Headless goal mode. The goal only counts as done when the verify command passes |
| `--max-turns N`, `--budget 2.50` | Limits for goal mode (turns, dollars) |
| `--allow "pytest"` | Headless: approve bash commands starting with this prefix (repeatable). Anything else is denied |
| `--yes` | Approve every tool call. Only use this in a sandbox |
| `--model`, `--effort` | Main model (default `claude-opus-5-5`) and effort (default `high`) |
| `--json` | Headless: print one JSON line with status, cost and answer |

## What's in it

| File | Lines | Job |
|---|---|---|
| `loop.py` | ~330 | Model calls, tool dispatch, subagents, compaction, `/goal`, cost |
| `cli.py` | ~210 | REPL, slash commands, headless mode |
| `tools.py` | ~150 | read, write, edit, bash, approval gate |
| `ext.py` | ~95 | Skills discovery, MCP stdio hub |
| `session.py` | ~85 | JSONL session files |
| `context.py` | ~40 | The frozen system prompt |

## How it works

**The loop.** Send the conversation, stream the reply, run any tools the model asked for, send the results back, and repeat until the model stops asking. All the hard parts are bookkeeping around that loop.

**The transcript is append-only.** On current Claude models a thinking block's signature is bound to everything sent before it: the system prompt, the tool list, and every earlier message. If you edit any of that and send the block back, newer accounts get a 400 error, and older ones lose the reasoning silently. So mini never edits history:

- The system prompt and tool list are built once, when the session starts. They are stored in the session file and replayed byte for byte on resume, even if `AGENTS.md` has changed since.
- Instructions that arrive later (a goal, clearing a goal) are appended as `{"role": "system"}` messages.
- Each request sets `prefix_mismatch_behavior: "drop_block"`. If something ever does break the rule, the API drops the affected thinking instead of failing, reports it in `input_transformations`, and mini logs and prints a warning.

**Sessions.** Each conversation is one JSONL file under `~/.mini/sessions/<project>/`. Each line is an event: `meta` (model, frozen prompt, tools), `msg`, `usage`, `compact` or `goal`. Resuming means reading the file. Nothing in it is ever rewritten.

**Compaction.** When the context passes 150K tokens, or when you run `/compact`, mini asks the API for a summary using [on-demand compaction](https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand). The returned block replaces the whole history, and a `compact` event starts a new segment in the session file, so older turns stay on disk. Two common shapes break thinking blocks and are avoided here: summarizing old turns while keeping recent ones verbatim, and compacting in the background.

**Every tool call gets an answer.** Failures, denials, interrupts and calls cut off by `max_tokens` all return a `tool_result` with `is_error: true`. Parallel calls are answered in one message. If any `tool_use` is left without a result, the session can't be resumed.

**Tools.** `read`, `write`, `edit` and `bash`, plus server-side `web_search`. `edit` refuses to change a file that was modified since the model last read it, and `write` won't overwrite a file the model hasn't read. Search, git and tests go through `bash`.

**Approvals.** Read-only commands such as `ls`, `grep` and `git diff` run without asking, as long as they contain no shell operators. Any other bash command, and any write outside the working directory, asks first. Headless mode denies anything not covered by `--allow`. This gate is not a sandbox.

**Memory and instructions.** At session start mini loads `AGENTS.md` (and only that file) plus `memory/INDEX.md`, which routes to area files such as `memory/decisions.md`. The model reads area files when a task touches them and updates them with `edit`. Changes take effect in the next session, because the prompt is frozen within a session.

**Skills.** mini looks for `skills/*/SKILL.md`, `.agents/skills/*/SKILL.md` and `~/.mini/skills/*/SKILL.md`. Each skill's name and description go into the system prompt. When a task matches a skill, the model reads its file with `read`. No special tool is needed.

**MCP.** List stdio servers in `mcp.json` (in the project directory or `~/.mini/`):

```json
{"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}}}
```

Their tools appear as `mcp__<server>__<tool>`. A server that fails to start is reported and skipped.

**Subagents.** The `task` tool runs the same loop with a fresh, empty history, read-only tools and a cheaper model (Sonnet 5.5, or Haiku 5.5 when asked). Only the subagent's final answer returns to the main conversation. Its full transcript is saved next to the parent session as `<id>-subN.jsonl`. The point of a subagent is to keep the main context clean. Speed isn't the goal.

**`/goal`.** This sets an objective and keeps the model going. After each turn the harness either sends "continue", or, if the model called `goal_done`, runs the `--verify` command and accepts the goal only if it passes. A failed check goes back to the model with its output. The loop also stops when the model writes `BLOCKED:` because it needs you, at the turn limit, at the budget, or on Ctrl-C. Goal state is saved in the session, so `/resume` followed by `/goal resume` picks it back up.

**Cost.** Each turn prints the running total, using the cache-read rates. Cache hits come from one top-level `cache_control` marker. Because the prefix never changes, the cache stays warm.

## Left out on purpose

A TUI, hooks, plugins, a sandbox, worktrees, checkpoints (use git), multiple providers, non-blocking subagents, tool search, model switching mid-session, streaming tool input, and ACP editor integration. Each one is a reasonable next step, and none is needed for the harness to do real work.

## Tests

```bash
uv run pytest -q
```

The tests run offline against a scripted fake client and check the invariants above: append-only requests, a result for every tool call, resume replaying the frozen prompt, the compaction swap, interrupt recovery, the goal state machine, subagent isolation, plus a real MCP server over stdio.
