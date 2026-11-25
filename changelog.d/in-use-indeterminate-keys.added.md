- **A key left executing by a killed process is indeterminate at once, and an
  admin can settle it (H14).** A process that starts serving a database no
  other process is serving marks every executing idempotency key
  indeterminate, where a retry was told `idempotency_in_progress` for about
  a minute first. `GET /idempotency-keys/indeterminate` lists the keys whose
  outcome is unknown with the request and sender of each, and
  `POST /idempotency-keys/{id}/resolve` settles one as `applied` (the key
  then refuses with the new code `idempotency_applied`) or `not_applied`
  (the key is forgotten); the trail records the decision. Tools
  `list_indeterminate_keys` and `resolve_idempotency_key`, admin only.
