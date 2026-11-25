- **The two command-migration debts can only go down, and the sprint commands
  paid theirs (G1).** `docs/COMMAND_MIGRATION.md`'s generated counts — commands
  that take a bare actor id, and writes that sit outside a command — each have a
  ceiling in `[tool.athena.command_debts]` of `pyproject.toml`, beside the
  coverage floors. A new `command-debts` stage in every gate profile and in CI
  (`scripts/generate_command_debtors.py --check`) fails when a count rises above
  its ceiling, naming the new debtor (the generator will not rewrite the table
  to absorb it), and when a count falls below it, with "lower the ceiling to N",
  so paid debt cannot grow back.

  The five sprint commands (create, edit, start, complete, delete) now take the
  resolved actor and check project visibility and creator-only management
  inside their own write transaction, so a caller reaching one directly is
  refused rather than trusted. REST and the cockpit keep their gate in front
  and answer exactly as before. The bare-actor count fell from 42 to 37 and its
  ceiling with it; no route, tool, setting or migration changed.
