- **A database file that disappears is not silently recreated, and readiness
  says why in the log (H16).** Once an app has started, a connection that
  finds its database file gone refuses instead of letting SQLite create an
  empty one: `/readyz` and every request answer `503 unavailable`. The log
  carries one line, `database unavailable: <reason>`, when the reason appears
  or changes, and one when it clears; before, `/readyz` failed with nothing
  logged. No response carries the reason or the path.
