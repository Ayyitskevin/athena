- **Athena reads the time from one clock, and the lease tests move it instead of
  rewriting a lease's expiry (G6).** `src/athena/core/clock.py` is that clock:
  the lease reads, a claim's and a renewal's expiry and the `lease_lapsed`
  observer (C4) all read it, and the lease SQL binds it instead of SQLite's
  `datetime('now')`; `utc_now()` and the packet clock read it too. In a running
  app it is the system clock and nothing can replace it, so no answer, row or
  event changes. Tests take the `clock` fixture, which holds it still and moves it
  by hand (and makes SQLite's `datetime('now')` follow it, for the lease
  triggers), and `support.lapse_lease(clock, ...)` lapses a lease by moving the
  clock past its expiry. A test that leaves the clock replaced fails.
