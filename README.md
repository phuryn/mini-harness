# mini

A minimal agent harness for Claude (and DeepSeek), small enough to read in one sitting. It has a tool loop, four local tools, sessions you can resume, compaction, skills, MCP servers, subagents, a `/goal` mode, a permission judge, and memory kept in markdown files. It has no UI framework, no plugin system and no dependencies beyond `anthropic` and `mcp`.

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
| `/new [model]` | Start a fresh session, on another model if given |
| `/effort [level]` | Show or change the effort for the next requests |
| `/permissions [mode]` | `ask`, `judge` or `yes`: who approves risky calls |
| `/sessions` | List recent sessions in this directory |
| `/resume [id]` | Resume the latest session, or one by id prefix |
| `/compact` | Replace the history with a summary now |
| `/goal <objective>` | Keep working until the objective is done; also `resume`, `clear`, `status` |
| `/exit` | Quit. Every message is already saved |

Ctrl-C stops the current turn and keeps the session valid.

| Flag | What it does |
|---|---|
| `-p "..."` | Run one prompt headless and print the answer |
| `-c` / `-r <id>` | Continue the latest session, or resume one |
| `--goal "..." --verify "pytest -q"` | Headless goal mode. The goal only counts as done when the verify command passes |
| `--max-turns N`, `--budget 2.50` | Limits for goal mode (turns, dollars) |
| `--allow "python3 -m pytest"` | Pre-approve bash commands that start with these words, or MCP tools whose name starts with this prefix (repeatable). Headless mode denies everything else |
| `--permissions ask\|judge\|yes` | Who approves risky calls: you (headless denies), the Jev judge, or nobody (`yes`, only in a sandbox) |
| `--model`, `--effort` | Main model (default `claude-opus-5-5`, or `deepseek-flash`) and effort (default `high`) |
| `--models` | List the models your key can use, then exit |
| `--json` | Headless: print one JSON line with status, cost and answer |

## What's in it

| File | Lines | Job |
|---|---|---|
| `loop.py` | 434 | Model calls, tool dispatch, subagents, compaction, `/goal`, the judge, providers, cost |
| `cli.py` | 196 | REPL, slash commands, headless mode |
| `tools.py` | 185 | read, write, edit, bash, approval gate |
| `ext.py` | 86 | Skills discovery, MCP stdio hub |
| `session.py` | 70 | JSONL session files |
| `context.py` | 25 | The frozen system prompt |

That's 998 lines of code (not counting blank lines, comments or docstrings), and about 150 of them are prompts, tool descriptions and schemas.

## How it works

**The loop.** Send the conversation, stream the reply, run any tools the model asked for, send the results back, and repeat until the model stops asking. All the hard parts are bookkeeping around that loop.

**The transcript is append-only.** On current Claude models a thinking block's signature is bound to everything sent before it: the system prompt, the tool list, and every earlier message. If you edit any of that and send the block back, newer accounts get a 400 error, and older ones lose the reasoning silently. So mini never edits history:

- The system prompt and tool list are built once, when the session starts. They are stored in the session file and replayed byte for byte on resume, even if `AGENTS.md` has changed since.
- Instructions that arrive later (a goal, clearing a goal) are appended as `{"role": "system"}` messages.
- Every conversation request sets `prefix_mismatch_behavior: "drop_block"`. If something ever does break the rule, the API drops the affected thinking instead of failing, reports it in `input_transformations`, and mini logs and prints a warning.

**Sessions.** Each conversation is one JSONL file under `~/.mini/sessions/<project>/`. Each line is an event: `meta` (model, frozen prompt, tools), `msg`, `usage`, `compact`, `goal` or `transform` (thinking blocks the API dropped). A line cut off by a crash is skipped. Resuming means reading the file. Nothing in it is ever rewritten.

**Compaction.** When the context passes 150K tokens (checked before each prompt and after each round of tool results, so a long tool loop can't outgrow the window), or when you run `/compact`, mini asks the API for a summary using [on-demand compaction](https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand). The returned block replaces the whole history, and a `compact` event starts a new segment in the session file, so older turns stay on disk. Two common shapes break thinking blocks and are avoided here: summarizing old turns while keeping recent ones verbatim, and compacting in the background. Server-side summarization also beats a client-side summary prompt here, because the server's summarizer can read the earlier thinking, and no summarization code ships with the harness.

**Every tool call gets an answer.** Failures, denials, interrupts and calls cut off by `max_tokens` all return a `tool_result` with `is_error: true`. Parallel calls are answered in one message. If an API call fails or you press Ctrl-C mid-stream, a short placeholder reply is appended so the transcript stays valid. A reply that pauses on a server tool (web search) is held back and committed only together with the reply that completes it, so a failure in between leaves no half-finished search in the history. A session that died mid-tool (closed terminal, crash) is repaired on resume, again only by appending.

**Fallbacks.** Requests to Opus and Sonnet set `fallbacks: "default"`. If the model declines a request partway through, the API continues on another model in the same response. mini then runs only the tool calls that come after the `fallback` marker, and drops the declined model's unpaired calls and thinking, as Anthropic's guide says. Cost is computed for each model separately.

**Tools.** `read`, `write`, `edit` and `bash`, plus server-side `web_search`. Every request sends each tool's name, description and schema. The descriptions warn about the usual mistakes: copying `read`'s line-number prefix into `edit`, editing a file without reading it first, an `old` string that isn't unique, interactive commands, and a subagent that can't see the conversation. mini's own tools set `strict: true`, so the API guarantees their arguments have the right shape and types; the code clamps numeric values. `read` pages long files before the 30,000-character output cap, and tells the model which offset comes next. When a call still fails, the error says what to do next ("Read the file before editing it", "`old` appears 3 times, add context"), so the model fixes it on the next step. `edit` refuses to change a file that was modified since the model last read it, keeps Windows line endings, and `write` won't overwrite a file the model hasn't read. `read` refuses special files and files over 5 MB. Search, git and tests go through `bash`.

**Approvals.** A short list of commands that can't run programs or write files (`ls`, `cat`, `head`, `tail`, `wc`, `grep`, `diff` and a few more) runs without asking, as long as it contains no shell operators or newlines. `find`, `rg` and `git` aren't on the list, because flags such as `-exec`, `--pre` and `--output` can run programs or write files. Any other bash command asks first. So does any write outside the working directory (every write, if you start mini in your home folder), and any write to a file that something else will run later: dot-folders such as `.git/` (git config can make `git status` run commands), `mcp.json`, `conftest.py`, `Makefile`, `package.json`, `pyproject.toml`, `setup.py`, any existing executable file, `GNUmakefile`, `justfile`, `tox.ini`, `noxfile.py`, and files named `HEAD` or `config`, because git treats any folder with those plus `objects/` and `refs/` as a repository. That list is a denylist of common runners, not a guarantee. The approval prompt shows the whole command, or the file content being written, with control characters escaped, and a command too long to review is denied. The check ignores case, because macOS file systems do. Every MCP tool call asks first. Headless mode denies anything not matched by `--allow`, which compares whole words, so `--allow "python3 -m pytest"` doesn't approve `python3 -c ...`. Writes elsewhere in the project are free, so allowing a test runner means the model can run code it wrote. `read` isn't restricted to the project either. This gate is not a sandbox.

**Memory and instructions.** At session start mini loads `AGENTS.md` (and only that file) plus `memory/INDEX.md`, which routes to area files such as `memory/decisions.md`. The model reads area files when a task touches them and updates them with `edit`. Changes take effect in the next session, because the prompt is frozen within a session.

**Skills.** mini looks for `skills/*/SKILL.md`, `.agents/skills/*/SKILL.md` and `~/.mini/skills/*/SKILL.md`. Each skill's name and description go into the system prompt. When a task matches a skill, the model reads its file with `read`. No special tool is needed.

**MCP.** List stdio servers in `mcp.json` (in the project directory or `~/.mini/`):

```json
{"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}}}
```

Their tools appear as `mcp__<server>__<tool>`, and each call asks for approval unless `--allow` covers it. A server that fails to start, or an `mcp.json` that isn't valid JSON, is reported and skipped.

**Subagents.** The `task` tool runs the same loop with a fresh, empty history, a short prompt of its own, `read`, `bash` and `web_search`, and a cheaper model (Sonnet 5.5, or Haiku 5.5 when asked). The subagent is told not to change files, and its `bash` calls go through the same approval gate. Only the subagent's final answer returns to the main conversation. Its full transcript is saved next to the parent session as `<id>-sub-<hex>.jsonl`. The point of a subagent is to keep the main context clean. Speed isn't the goal.

**`/goal`.** This sets an objective and keeps the model going. After each turn the harness either sends "continue", or, if the model called `goal_done`, runs the `--verify` command and accepts the goal only if it passes. A failed check goes back to the model with its output. The loop also stops when the model writes `BLOCKED:` because it needs you, at the turn limit, at the budget, or on Ctrl-C. Goal state is saved in the session, so `/resume` followed by `/goal resume` picks it back up. When a goal stops, the model is told that goal mode is off, even across a restart. `--verify` runs in your shell, so a verify command that runs the project's tests also runs code the model wrote.

**Models and providers.** `--model` (or `/new <model>`) picks the model for a session, and `mini --models` lists what your key can use. The model stays fixed for the whole session, because a switch would drop the replayed thinking. Effort can change at any time with `/effort`: it's a setting on each request, not part of the history. Each provider has its own levels (Claude: low to max; DeepSeek: low, high, max).

DeepSeek V4.1 Flash (`--model deepseek-flash`, key in `DEEPSEEK_API_KEY`) runs through DeepSeek's Anthropic-format endpoint, so the loop, tools, sessions and web search are the same code. What it lacks is Anthropic's server-side compaction and fallbacks. For DeepSeek, mini compacts on its own: it asks the model for a summary and starts a new segment from it. Subagents run on the same model. Any other endpoint that speaks Anthropic's format can go in `~/.mini/models.json`, with its context window and compaction threshold:

```json
{"my-model": {"base_url": "https://example.com/anthropic", "key": "MY_API_KEY", "window": 262144,
              "compact_at": 150000, "search": 0, "efforts": ["low", "high"], "prices": [0.5, 2, 0.05, 0.5]}}
```

Keys stay in environment variables, never on the command line, where shell history and `ps` would keep them. A model whose key is missing refuses to start, so the Anthropic key is never sent to another endpoint. An OpenAI-format endpoint would need a translation layer for messages, tools and thinking, which is more code than the rest of this file.

**The permission judge.** With `--permissions judge` (or `/permissions judge`), a risky call goes to [Jev](https://www.productcompass.pm/p/jev-decision-model), a decision model on OpenRouter, before it would reach you. Jev answers allow or deny with a confidence. Below 0.8 the call asks you, and in headless mode it's denied. If Jev can't be reached, the call is denied. A denial goes back to the model with the reason, so it can change course. Jev sees your own messages (including your answers to `ask_user`), the active goal, and the whole action, nothing else: not the model's reasoning, not tool output, and not messages the harness writes, such as test output from `--verify`, because those can carry instructions aimed at whoever reads them. An action too long to review (over 4,000 characters) is denied before Jev sees it. Writes that need approval (outside the project, or files that run later) always go to you, never to Jev. A check costs about $0.00002. It's still a model judging a model, not a sandbox.

**Asking you.** The `ask_user` tool lets the model ask a question mid-task. In headless runs nobody answers, so the model is told to make its best assumption and say what it assumed.

**Cost.** Each turn prints the running total, using the cache-read rates. Cache hits come from one top-level `cache_control` marker. Because the prefix never changes, the cache stays warm.

## Left out on purpose

A TUI, hooks, plugins, a sandbox, worktrees, checkpoints (use git), OpenAI-format providers, non-blocking subagents, tool search, model switching mid-session, streaming tool input, and ACP editor integration. Each one is a reasonable next step, and none is needed for the harness to do real work.

## Tests

```bash
uv run pytest -q
```

The tests run offline against a scripted fake client and check the invariants above: append-only requests, a result for every tool call, resume replaying the frozen prompt, the compaction swap, interrupt recovery, the goal state machine, subagent isolation, plus a real MCP server over stdio.
