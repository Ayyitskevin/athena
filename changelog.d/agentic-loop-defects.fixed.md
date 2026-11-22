- **Three defects in the documented agent loop.** A gated write retried under
  its idempotency key after the operator approved replayed the stored 202 and
  never applied; the same key now applies once and that result is the receipt.
  `claim_next_ready` handed an agent back the issue it had just completed or
  yielded with a question; it is now held back from that account until someone
  else answers. A desk cursor could be advanced past the newest event, which
  blinded the change signal; that is refused as `cursor_ahead`, and migration
  0085 repairs cursors already past the end.
