- **The container first run can be completed as written.** `compose.yaml` now
  forwards `ATHENA_BOOTSTRAP_TOKEN` from the shell (named without a value, so it
  stays unset in the container when the shell has none); before, the bootstrap
  run its own comment described never received the token. `docs/DOCKER.md` now
  creates the first admin from inside the bootstrap container with the image's
  Python, since the server listens on the container's loopback, no port is
  published and the image has no `curl`; gives `athena-backup` the two
  arguments it needs; and says that the image's `HEALTHCHECK` asks `/healthz`
  (liveness) while readiness is `/readyz`, as `OPERATIONS.md` now also says. A
  test parses every `athena-*` invocation in `DOCKER.md` and `OPERATIONS.md`
  with the command's own argument parser. No container was run to check this.
