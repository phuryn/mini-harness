# Terminal-Bench 2.0 sample · Claude Haiku 5.5

The official 10-task sample of [Terminal-Bench 2.0](https://www.tbench.ai/) (`terminal-bench-sample@2.0`), five runs per harness, Claude Haiku 5.5 at high effort on Anthropic's API, one account.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../../docs/img/terminal-bench-dark.png">
  <img src="../../docs/img/terminal-bench-light.png" alt="Terminal-Bench 2.0 sample · Claude Haiku 5.5: mean tasks passed per harness">
</picture>

| Harness | Version | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | Mean, of 10 | Agent min per run | Cost per run |
|---|---|---|---|---|---|---|---|---|---|
| Mini Harness | 1.0.0 | 9 | 8 | 10 | 9 | 10 | **9.2** | 31 | $0.12 |
| Pi | 1.5.0 | 9 | 9 | 8 | 9 | 9 | **8.8** | 35 | $0.09 |
| mini-swe-agent | 2.4.6 | 8 | 9 | 8 | 9 | 10 | **8.8** | 38 | $0.19 |
| Hermes Agent | 0.22.7 | 7 | 9 | 9 | 8 | 9 | **8.4** | 44 | ~$0.10 |
| Claude Code | 2.1.317 (2.1.318 on 6 of 50 task runs) | 8 | 9 | 8 | 9 | 8 | **8.4** | 33 | $0.19 |
| OpenCode | 1.19.5 | 8 | 9 | 7 | 8 | 9 | **8.2** | 36 | $0.11 |
| Terminus 2 | 2.0.0 | 8 | 8 | 8 | 8 | 8 | **8.0** | 51 | $0.13 |
| Kilo Code | 7.8.18 | 8 | 7 | 9 | 7 | 7 | **7.6** | 39 | $0.13 |

## Setup

- Runner: [Harbor](https://github.com/laude-institute/harbor) 0.25.0, Docker, each task's own time limit and resources.
- When: 3–5 Nov 2026.
- Effort: `--effort high`; OpenCode as `variant=high`; Hermes Agent from its config file.
- Cost: what each harness reports. Hermes Agent reports none, so its runs are priced from its tokens at Haiku 5.5's list price (marked ~; an estimate that may be low).
- Harnesses: Mini Harness 1.0.0, Pi 1.5.0, mini-swe-agent 2.4.6, Hermes Agent 0.22.7, Claude Code 2.1.317 (2.1.318 on 6 of 50 task runs), OpenCode 1.19.5, Terminus 2 2.0.0, Kilo Code 7.8.18.

## Files

- `trials.csv`: one row per run of a task, for every harness. `passed` is 1 when the task's hidden tests passed;
  `agent_seconds` is the agent's own time (container setup excluded); `cost_usd` is what the harness reported; for Hermes Agent, which reports none, its tokens at Haiku 5.5's list price (`cost_estimated` = 1, an estimate that may be low). `cost_usd` is blank
  when a run recorded no cost or token counts (Hermes Agent 6 runs). `error` names how a run ended when it did not end on its
  own (`AgentTimeoutError` = stopped at the task's time limit).
- `summary.csv`: one row per harness, the numbers in the table above. Cost per run is the mean cost of the runs
  that recorded one, times the number of tasks.
