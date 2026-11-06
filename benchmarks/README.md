# Benchmarks

Results of Mini Harness against other agent harnesses on public benchmarks, one folder per benchmark and model, with every counted run in `trials.csv`. ([`../smoke-test/`](../smoke-test/) is different: Mini's own five-task check, run while developing it.)

| Benchmark | Model | Tasks × runs | Harnesses | Folder |
|---|---|---|---|---|
| Terminal-Bench 2.1 | Claude Haiku 5.5 | 89 × 3 | 9 | [terminal-bench-2.1--claude-haiku-5.5](terminal-bench-2.1--claude-haiku-5.5/) |
| Terminal-Bench 2.0 sample | Claude Haiku 5.5 | 10 × 5 | 8 | [terminal-bench-2.0-sample--claude-haiku-5.5](terminal-bench-2.0-sample--claude-haiku-5.5/) |
| Terminal-Bench 2.0 sample | DeepSeek V4.1 Flash 1017 | 10 × 5 | 8 | [terminal-bench-2.0-sample--deepseek-v4.1-flash-1017](terminal-bench-2.0-sample--deepseek-v4.1-flash-1017/) |
| Bug Hunt Bench | DeepSeek V4.1 Flash 1017 | 105 bugs × 5 | 10 | [bug-hunt-bench--deepseek-v4.1-flash-1017](bug-hunt-bench--deepseek-v4.1-flash-1017/) |
