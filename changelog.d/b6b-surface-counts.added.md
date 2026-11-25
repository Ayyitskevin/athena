- **The surface's size is held to a tracked file, and growth says what it earns
  (B6b).** `scripts/check_surface_counts.py` measures the routes, MCP tools and
  tables (417, 146 and 73 today) and fails unless `scripts/surface_counts.toml`
  accounts for each: a baseline plus one `[[change]]` per later change, with
  `earns` on every addition. The product gate and CI run it, and the prune
  ledger points to it instead of keeping its own stale counts.
