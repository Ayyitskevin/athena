- **The background loops run in one process, and only once it is bound
  (H13).** A duplicate `athena-serve` start no longer runs the webhook and
  automation loops before it fails on the port. Only the process holding an
  exclusive lock on `<database>.loops.lock` runs them; a second process on
  the same database serves requests, logs one line saying it runs no loops,
  and `GET /readyz` reports `background_loops` as `running`, `standby` or
  `disabled`.
