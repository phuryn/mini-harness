# Terminal-Bench 2.1 · Claude Haiku 5.5

All 89 tasks of [Terminal-Bench 2.1](https://www.tbench.ai/), three runs per harness, Claude Haiku 5.5 at high effort on Anthropic's API, one account.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../../docs/img/terminal-bench-2.1-dark.png">
  <img src="../../docs/img/terminal-bench-2.1-light.png" alt="Terminal-Bench 2.1 · Claude Haiku 5.5: mean tasks passed per harness">
</picture>

| Harness | Version | Run 1 | Run 2 | Run 3 | Mean, of 89 | Agent min per run | Cost per run |
|---|---|---|---|---|---|---|---|
| Kilo Code | 7.8.18 | 72 | 78 | 72 | **74.0** | 624 | $2.39 |
| Claude Code | 2.1.317 | 74 | 72 | 72 | **72.7** | 633 | $5.15 |
| Mini Harness | 1.0.3 | 71 | 71 | 74 | **72.0** | 571 | $3.28 |
| Hermes Agent | 0.23.0 (0.22.7 on 13 of 267 task runs) | 68 | 72 | 73 | **71.0** | 798 | ~$1.69 |
| Pi | 1.5.0 | 70 | 72 | 69 | **70.3** | 636 | $4.75 |
| mini-swe-agent | 2.4.6 | 67 | 70 | 72 | **69.7** | 674 | $5.75 |
| OpenCode 2 | 2.2.5 | 67 | 68 | 72 | **69.0** | 610 | $5.17 |
| Terminus 2 | 2.0.0 | 68 | 70 | 65 | **67.7** | 745 | $11.61 |
| OpenCode | 1.19.5 | 57 | 66 | 57 | **60.0** | 577 | $1.88 |

## Setup

- Runner: [Harbor](https://github.com/laude-institute/harbor) 0.25.0, dataset `terminal-bench/terminal-bench-2-1`, Docker, each task's own time limit and resources.
- Machines: DigitalOcean droplets (8 vCPU, 16 GB), nine per wave and two waves of the same size, six runs at a time on each; every harness ran the same task list on each machine of its wave.
- When: Kilo Code, Claude Code, Hermes Agent and Pi ran together on 5–6 Nov 2026; Mini Harness 1.0.3 ran right after on the same machines, then mini-swe-agent, OpenCode, OpenCode 2 and Terminus 2 on nine new machines of the same size, on 6 Nov, with the same tasks and settings.
- Effort: `--effort high`; OpenCode as `variant=high`; OpenCode 2 as the model's `#high` variant (Harbor installs only OpenCode 1.x, so OpenCode 2 ran through a small adapter on Harbor's OpenCode one); Hermes Agent from its config file.
- Cost: what each harness reports. Hermes Agent reports none, so its runs are priced from its tokens at Haiku 5.5's list price (marked ~; an estimate that may be low).
- Harnesses: Kilo Code 7.8.18, Claude Code 2.1.317, Mini Harness 1.0.3, Hermes Agent 0.23.0 (0.22.7 on 13 of 267 task runs), Pi 1.5.0, mini-swe-agent 2.4.6, OpenCode 2 2.2.5, Terminus 2 2.0.0, OpenCode 1.19.5.

## Files

- `trials.csv`: one row per run of a task, for every harness. `passed` is 1 when the task's hidden tests passed;
  `agent_seconds` is the agent's own time (container setup excluded); `cost_usd` is what the harness reported; for Hermes Agent, which reports none, its tokens at Haiku 5.5's list price (`cost_estimated` = 1, an estimate that may be low). `cost_usd` is blank
  when a run recorded no cost or token counts (Hermes Agent 64, Pi 3 runs). `error` names how a run ended when it did not end on its
  own (`AgentTimeoutError` = stopped at the task's time limit).
- `summary.csv`: one row per harness, the numbers in the table above. Cost per run is the mean cost of the runs
  that recorded one, times the number of tasks.
