- **The changed profile finds the tests that reach code through the app from
  recorded coverage, not from a hand-kept list of file names (G7).**
  `scripts/test_map.json` maps each source file under `src/athena` to the test
  files that ran it, less those the import graph already selects;
  `scripts/generate_test_map.py` writes it from per-test coverage contexts, and
  `scripts/select_tests.py` reads it in place of its fifteen hand entries for
  Python modules (four remain, for templates, static assets, field-guide pages
  and documents, which coverage does not measure). A route used as setup by a test named for
  something else is now selected. A file most recorded tests run (the
  middleware, the request log) is left out of the map and a change to it runs
  the full suite; a module the map has not recorded yet does too. A new weekly
  `Test map` workflow records the whole suite with contexts and fails, printing
  the diff and uploading the new map, when the committed one is stale; it has
  read-only permissions and never commits. Regenerate locally with
  `.venv/bin/python scripts/generate_test_map.py --record`. The committed map
  was recorded from part of the suite and the first scheduled run replaces it.
  No route, tool, setting or migration changed.
