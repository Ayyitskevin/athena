- **A HOLD or BLOCKED report asks its author for the rule that would have
  prevented it (W4).** The `complete_claim` answer's `next` for a report of
  `HOLD` or `BLOCKED` names the call that records a learning,
  `record_run_learning(issue_id, summary)`.
  Any other completion is answered as before. The conductor recipe asks for
  one rule per HOLD. Nothing is recorded or promoted by reporting.
