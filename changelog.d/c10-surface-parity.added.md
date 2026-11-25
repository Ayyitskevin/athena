- **The gate fails on a command only one transport reaches, unless it is
  declared with a reason (C10).** `scripts/check_surface_parity.py`, a new
  `surface-parity` stage in every gate profile and in CI, finds each command
  function reached from the cockpit (`web/`) but not the REST API (`*_api.py`,
  `api.py`), or the other way round, following helper modules to any depth. Each
  one must have a row in `scripts/surface_parity.toml` saying what it is: a
  pair (the same capability under another name), a read, one transport's by
  design, reached elsewhere, or a missing twin someone needs. A row naming a
  function that is gone, or that both transports reach now, fails as stale.

  Today 42 functions are one-sided, in 28 rows. Three are missing twins, none
  built here: assign-to-seat with the Buzz radio has no REST route, and the
  cockpit cannot resume a claim handoff or edit a sprint. MCP is a client of
  REST and is not compared. No route, tool or page changed.
