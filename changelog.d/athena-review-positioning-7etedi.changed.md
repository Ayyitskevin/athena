- **The documentation index, the settings tables and the command-migration
  debtor list are generated or asserted, not kept by hand.** `docs/INDEX.md` now
  reaches every document under `docs/` (it had dropped five and the decision
  records), and a test fails on the next one it misses or on a link that does not
  resolve. Every `ATHENA_*` variable the code reads is declared once, in
  `athena.config.SETTINGS`, with its default, description and whether it is a
  secret; a static test fails on a read the registry does not declare, and the
  tables in `docs/OPERATIONS.md` are generated from it (they had omitted 21 of 56
  variables, among them dispatch, the Buzz radio, the worker registry and the MCP
  client's own). `docs/COMMAND_MIGRATION.md` gains generated tables of commands
  that still take a bare actor id and of writes outside a command; its "None
  known" rows for sprints and API tokens were wrong and are corrected. No setting
  changed its default, parsing or error text.
