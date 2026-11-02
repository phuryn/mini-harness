# mini

A minimal agent harness for Claude (and other models that speak Anthropic's message format), small enough to read in one sitting: about 1,075 lines of Python, with no dependencies beyond `anthropic` and `mcp`.

It has a tool loop, five local tools plus web search, sessions you can resume, compaction, Agent Skills, MCP servers, subagents, a `/goal` mode with an independent reviewer, permission prompts with an optional AI judge, and memory kept in markdown files. It's built to show what a harness actually has to do, and which parts you can leave out.

## How it compares

| Harness | Size | Tools | Sessions | Compaction | Permissions | Subagents | Goal mode | MCP |
|---|---|---|---|---|---|---|---|---|
| **mini** | 1,075 lines, Python | 5 + web search, subagent, ask user | yes | yes | yes, plus judge | yes | yes, with reviewer | yes |
| mini-swe-agent | ~100-line agent class, Python | terminal only | no | no | runs in a container | no | no | no |
| nanocode | ~250 lines, one Python file | 6 | no | no | no | no | no | no |
| Pi | ~1,800-line agent loop; ~190K lines in the repo (TypeScript) | 4 (+3 optional) | yes, branching | yes | no, by design | no, by design | no | yes |
| Claude Code, Codex CLI | full products | many | yes | yes | yes | yes | yes | yes |

As of November 2026. Pi is built to be extended; mini is built to be read. Smaller harnesses do real work, but none of the ones above covers crash recovery, an append-only transcript, permissions and checked goals together.

## Quick start

```bash
git clone git@github.com:phuryn/mini-harness.git ~/mini && cd ~/mini && uv sync
export ANTHROPIC_API_KEY=sk-ant-...
cd ~/some/project
uv run --project ~/mini mini                 # interactive
uv run --project ~/mini mini -p "explain this repo in five bullets"
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Optional keys: `DEEPSEEK_API_KEY` (DeepSeek), `OPENROUTER_API_KEY` (the permission judge).

## Using it

| Command | What it does |
|---|---|
| `/new [model]` | Start a fresh session, on another model if given |
| `/effort [level]` | Show or change the effort for the next requests |
| `/permissions [mode]` | `ask`, `judge` or `yes`: who approves risky calls |
| `/sessions`, `/resume [id]` | List sessions in this directory; resume the latest or one by id prefix |
| `/compact` | Replace the history with a summary now |
| `/dream` | Tidy `memory/`: merge duplicates, drop what's stale |
| `/goal <objective>` | Keep working until the objective is done; also `resume`, `clear`, `status` |
| `/exit` | Quit. Every message is already saved |

Ctrl-C stops the current turn and keeps the session valid.

| Flag | What it does |
|---|---|
| `-p "..."` | Run one prompt headless and print the answer (`--json` for one JSON line with status and cost) |
| `-c` / `-r <id>` | Continue the latest session, or resume one |
| `--model`, `--effort` | Model (default `claude-opus-5-5`) and effort (default `high`); `--models` lists what your keys can use |
| `--allow "python3 -m pytest"` | Pre-approve bash commands starting with these words, or MCP tools starting with this prefix (repeatable). Headless mode denies everything else |
| `--permissions ask\|judge\|yes` | Who approves risky calls: you, the Jev judge, or nobody (`yes`: only in a sandbox) |
| `--goal "..." --verify "pytest -q"` | Headless goal mode; done only when the verify command passes and a fresh reviewer agrees |
| `--max-turns N`, `--budget 2.50` | Limits for goal mode |

**Project files it reads.** `AGENTS.md` (instructions), `memory/INDEX.md` (memory, which the model keeps up to date), skills in `skills/`, `.claude/skills/` or `.agents/skills/` (plus `~/.mini/skills/`), and MCP servers in `mcp.json` or `.mcp.json` (plus `~/.mini/mcp.json`). Skills and MCP use the same formats as Claude Code, so existing ones work as they are. Only each skill's name and description go into the prompt; the model reads the full `SKILL.md` when a task matches.

**MCP.** Servers run over stdio, as any command:

```json
{"mcpServers": {
  "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
  "linear": {"command": "npx", "args": ["-y", "mcp-remote", "https://mcp.linear.app/mcp"]}
}}
```

Hosted servers with OAuth go through `mcp-remote`, which runs the login in your browser once and keeps the token. Tested with Linear. Every MCP call asks first unless `--allow` covers it (e.g. `--allow mcp__linear__list_` for Linear's read-only list tools).

## Adding a model

**A new Claude model** works by name: `--model claude-<name>`. Add its prices (in $ per million tokens: input, output, cache read, cache write) to `PRICES` in `src/mini/loop.py`, or the cost shown falls back to Opus 5.5 rates. Claude models get everything: adaptive thinking, server-side compaction, fallbacks on refusals (not on Haiku), and $0.01 web search. The context window is taken as 1M tokens; compaction starts at 150K (`MINI_COMPACT_AT` changes it).

**Any other model** must serve Anthropic's Messages format (DeepSeek, Kimi, GLM and others offer such an endpoint). Add it to `~/.mini/models.json`:

```json
{"my-model": {
  "base_url": "https://example.com/anthropic",
  "key": "MY_API_KEY",
  "window": 262144,
  "compact_at": 150000,
  "search": 0,
  "efforts": ["low", "high"],
  "prices": [0.5, 2, 0.05, 0.5]
}}
```

| Field | Meaning |
|---|---|
| `base_url` | The provider's Anthropic-format endpoint |
| `key` | Name of the environment variable that holds the key (never the key itself) |
| `window` | Context window in tokens (shown in the status line) |
| `compact_at` | Input tokens at which mini summarizes the conversation; keep it well below `window` |
| `search` | $ per web search, for the cost display |
| `efforts` | Effort levels the provider accepts |
| `prices` | $ per million tokens: input, output, cache read, cache write |

Then run `mini --model my-model`. What changes for a non-Claude model: no thinking-block binding, betas or fallbacks are sent, compaction is done by mini itself (it asks the model for a summary) instead of the server, and subagents run on the same model. DeepSeek V4.1 Flash is built in as `deepseek-flash`. OpenAI-format endpoints aren't supported; they'd need a translation layer for messages, tools and thinking.

## How it works

- **The loop.** Send the conversation, stream the reply, run the tools the model asked for, send the results back, repeat until it stops asking. The rest is bookkeeping around that loop.
- **Append-only transcript.** The system prompt and tool list are frozen at session start; nothing already sent is ever edited. Current Claude models bind thinking blocks to everything before them, so an edit would break the conversation. Later instructions are appended as system messages.
- **Sessions.** One JSONL file per conversation under `~/.mini/sessions/<project>/`. Resuming means reading it back; a crash mid-tool is repaired on resume, by appending.
- **Every tool call gets an answer**, including failures, denials and interrupts, so the transcript stays valid.
- **Tools.** `read`, `write`, `edit`, `bash`, `find`, plus `web_search`, `task` (a subagent with a fresh context that returns only its answer), `ask_user` (a question, optionally with numbered options) and `goal_done`. Errors tell the model what to do next.
- **Permissions.** Read-only commands and edits inside the project run freely. Other bash commands, MCP calls, writes outside the project and writes to files that run later (`.git/`, `Makefile`, `package.json`...) ask first, or go to [Jev](https://www.productcompass.pm/p/jev-decision-model), a small decision model, in `judge` mode. This is a gate, not a sandbox.
- **`/goal`.** The model keeps working until it calls `goal_done`. The harness then runs `--verify`, and a reviewer subagent with an empty context checks the work. The worker never grades itself.
- **Cost.** Every request is priced from its token usage and summed; the running total shows after each turn.

## Left out on purpose

A full-screen UI, hooks, plugins, a sandbox, checkpoints (use git), forked or parallel subagents, OpenAI-format providers, switching models mid-session, and editor integration.

## Tests

```bash
uv run pytest -q
```

129 offline tests against a scripted fake client and a real MCP stdio server. They check the invariants above: append-only requests, an answer for every tool call, resume, compaction, interrupts, goal mode, subagent isolation. `AGENTS.md` has the rules for changing the code.
