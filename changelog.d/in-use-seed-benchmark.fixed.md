- **`scripts/seed_benchmark.py` runs again.** It stopped with a `TypeError`
  when the writers it calls took a required `commit` keyword, so the numbers the
  performance notes call reproducible could not be reproduced. A test now runs
  it at its smallest size and reads back what it wrote. Every script a document
  shows in code now answers `--help` with its usage and exits 0 without doing
  its work (eight of them used to ignore the flag and run, two of those
  rewriting a generated document), and a test holds them to it.
