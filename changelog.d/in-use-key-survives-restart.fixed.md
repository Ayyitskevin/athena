- **A restarted session replays its own idempotency key (F14).** A key was
  matched on the run headers as well as the request, and `athena-mcp` mints a
  run id for each session, so a session that crashed and restarted retried
  its in-flight write under a new run and was refused 409
  `idempotency_mismatch`: the one retry a key exists to make safe. The run
  headers (`X-Athena-Run`, `X-Athena-Parent-Run`, `X-Athena-Fork-From-Event`)
  are no longer part of the match. The retry replays the first answer,
  nothing is written twice, and the event stays attributed to the run that
  made it. A key is still matched on the method, path, query, body, `Accept`,
  content type and encoding, and conditional headers, and still belongs to
  one credential. A receipt stored before this release matches a retry that
  carries the same run headers, as it did then; a process still running the
  older code refuses a receipt this release stored, which fails closed. An
  MCP tool answer that is a stored one now carries `_replayed: true`, as REST
  already sends `Idempotent-Replay: true`: a key reused for an identical call
  in a later session, which the run match used to refuse, is answered from
  the receipt and says so.
