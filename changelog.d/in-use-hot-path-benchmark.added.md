- **A committed seeder and benchmark for the hot paths.**
  `scripts/seed_benchmark.py --shape fleet` writes a workspace a fleet has
  worked in, through the application commands, at 1,000, 10,000 or 50,000
  issues; `scripts/benchmark_hot_paths.py` times the desk, pickup, the inbox and
  the other hot paths over HTTP against a copy of it, and cleans up its server
  and copy however it is stopped. `--record` writes the 10,000-issue medians to
  `docs/PERFORMANCE_MEDIANS.json` and refuses on a busy machine or a checkout
  that is not a commit; the file holds none until they are recorded.
  `docs/PERFORMANCE.md` says how to run both and why the gate holds query plans
  rather than timings.
