# Smoke test

Mini's own end-to-end check, run while developing it: five small tasks, each in its own folder with a pass/fail
check, run headless against a real model. Not a benchmark of other harnesses (those results are in
[`../benchmarks/`](../benchmarks/)).

| Task | What it checks |
|---|---|
| bugfix | finds and fixes the bugs in `dates.py` until its tests pass |
| feature | adds a `--top N` option to `wordcount.py`, with a test |
| research | answers four questions from three docs, citing the source, where the facts changed between docs |
| goal | implements `slugify` in goal mode, with the tests as the verifier |
| compaction | reads six modules with a tiny compaction threshold, then totals their constants correctly |

    uv run python smoke-test/run.py --harness mini          # or mini-deepseek, claude, both; --runs N; --only id,id

Each run copies the task to a temp folder, runs the harness, runs the task's check and appends one line to
`results.jsonl`.
