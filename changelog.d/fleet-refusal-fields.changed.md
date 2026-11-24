- **A refusal carries its facts as fields, and every registered `next` makes
  progress or waits for its change (F11).** `claim_held` carries `holder`,
  `expires_at` and `events_after` (filled for a caller who could read that
  lease), a holder's `lease_generation_required` its `generation` at every site,
  `rate_limited` a `rate_limit` object, `payload_too_large` its `limit_bytes`, a
  capacity refusal its `limit`, and `approval_required` and `fence_overlap` an
  `events_after` cursor. `claim_held`, `fence_overlap` and `approval_required`
  now name a `wait_for_events` from that cursor, which parks until the holder or
  the operator acts. `scope_denied`, `lease_generation_required` and
  `blocked_issue_close_policy` name no `next`: their old one only restated the
  refusal. `tests/test_refusal_next.py` builds the state behind every code with a
  `next`, follows it as the refused agent and requires progress, or a wait that
  parks, answers once the test changes the state, and lets the call succeed.
