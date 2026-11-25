- **One log line per request, and an id on every response (H15).** Each
  request logs `request id=... actor=... method=... route=... status=...
  code=... ms=...` on `athena.request`; the response carries the id as
  `X-Request-Id`, and a caller's own valid id is kept. An unhandled error is
  logged once with that id and one traceback, where the traceback printed
  three times and named no request. A failed webhook delivery is logged once
  per attempt. `athena-serve` turns off the web server's own access line.
