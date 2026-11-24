- **The credential that sent an idempotency key can read its state (A11).**
  `GET /idempotency/{key}` and the `get_idempotency_key` tool (in every
  profile) say what a retry under the key would meet: `unused`,
  `in_progress`, `completed`, `indeterminate` or `authorization_changed`,
  with the keyed request's method and path, the stored answer's status when
  it completed, and `retry` in words. The read changes nothing: it claims,
  replays, purges and expires nothing. Keys are stored per credential, so
  any other caller's read of the same text answers `unused`. The agent guide
  has a table of the five states and what to do in each.
