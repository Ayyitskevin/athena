- **A lapsed lease is an event.** When a lease expires with nobody settling it,
  the first read that observes it records one `lease_lapsed` event for that
  lease generation: `GET /events` (`wait_for_events`) on entry, and
  `GET /attention/ranking` after it reads. The issue's assignee and the account
  that delegated the work are notified and see a `claim_needs_attention` row, so
  a conductor in a wait loop learns of the lapse within one wait. Nothing is
  released or swept, automation never fires on the verb, and an account that
  cannot see the issue learns nothing. See `docs/ACTIVE_WORK.md`.
