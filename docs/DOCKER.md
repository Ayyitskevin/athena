# Running Athena in a container

The image runs the same `athena-serve` launcher as a local install, under the same
fail-closed deployment gate. Nothing about being in a container widens what Athena
will bind to or who it will answer.

That last sentence is the one that shapes everything below, so it is worth stating
plainly before the commands: **there is no network mode that binds a routable
address.** `ATHENA_NETWORK_MODE=local` permits loopback only; `tailnet` additionally
permits Tailscale's own ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`). There is no
`public` value, by design. A container is a network namespace, so "loopback" means
*inside the container* — which is why `compose.yaml` publishes no port, and why
adding one would not help.

## First run

`athena-serve` refuses a database that does not exist. Creating one, and opening
first-admin bootstrap, is a separate and explicit act — the container does not do it
for you on a fresh volume, because an image that quietly bootstrapped would undo the
gate the launcher exists to hold.

```bash
# One time, per volume.
export ATHENA_BOOTSTRAP_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
docker compose run --rm athena --bootstrap
```

That migrates the database and serves with bootstrap enabled. Now create the first
admin against the token (see [OPERATIONS.md](OPERATIONS.md) for the request) —
**this step is not optional.** `athena-serve` refuses to start a database that has
no active administrator, so a volume that was bootstrapped but never given one
cannot be brought up again; you would have to delete it and start over. CI performs
exactly this sequence, which is how that sharp edge was found.

Then stop the container and bring it up normally — **without** the token in the
environment:

```bash
unset ATHENA_BOOTSTRAP_TOKEN
docker compose up -d
```

## Reaching it

| shape | works | why |
|---|---|---|
| `docker exec` / the image's `HEALTHCHECK` | **yes** | loopback inside the namespace is where a `local` bind lives |
| `ports: "8000:8000"` with `local` mode | **no** | publishes a host port to a container address the process is not bound to; connections hang rather than refuse |
| `network_mode: host` + `tailnet` mode + an explicit `--host <tailscale-ip>` | **yes** | the container shares the host's namespace, so the tailnet address is real and bindable |
| a Tailscale sidecar giving the container its own `100.64.x.x` | **yes** | same reason, with the address inside the namespace |
| exposing to a LAN or the internet | **unsupported** | no mode binds it, and no reverse-proxy shape is supported yet — see F-1.2 in the performance guide, which is an open operator decision |

The tailnet shape needs settings that `local` derives for you and that fail closed
when absent — `ATHENA_ALLOWED_AUTHORITIES` must name the exact `host:port` clients
will use, and `ATHENA_ANON_RATE_LIMIT_PER_MINUTE` must be positive. The commented
block in `compose.yaml` has the full form.

## What lives in the volume

`/var/lib/athena` holds the SQLite database and the attachment blobs together, so a
backup is one path and is always internally consistent. Back it up with
`athena-backup` rather than by copying the file out from under a running process:

```bash
docker compose exec athena athena-backup /var/lib/athena/backup.db
```

## What the image contains

A base install — not the `[mcp]` extra. The image's job is `athena-serve`; the MCP
server is an optional runtime component and pulling its dependency surface into
every deployment is the wrong default. `athena-mcp` therefore is not available in
this image; install Athena with `pip install 'athena[mcp]'` where you need it.

The wheel is built in a first stage and installed under `constraints/ci-py312.txt`
in the second, so the runtime image carries no build tooling and no source tree, and
the dependency graph is the one CI pins.

It runs as uid/gid `10001` (`athena`), never root. A **named** volume inherits that
ownership automatically; a **bind mount** of a host directory does not, so chown it
to `10001:10001` first or the process cannot write.
