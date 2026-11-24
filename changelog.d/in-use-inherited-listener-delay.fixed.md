- **A server started on an inherited socket answers without a 40 ms delay.**
  `athena-serve --fd N` (socket activation) answered every request about
  40 ms late: 44 ms for `/healthz` against 2 ms for a server started with
  `--host` and `--port`. The server library wraps an inherited descriptor as a
  UNIX-domain socket, so `TCP_NODELAY` was never set on its connections and
  each answer waited for the client's delayed acknowledgement. The launcher
  sets the option on the inherited listener, which every connection it accepts
  takes. Found by the hot-path benchmark, which starts its server this way.
