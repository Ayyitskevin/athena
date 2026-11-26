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
# One time, per volume. Leave this running in its own terminal.
export ATHENA_BOOTSTRAP_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
docker compose run --rm --name athena-bootstrap athena --bootstrap
```

`compose.yaml` names `ATHENA_BOOTSTRAP_TOKEN` without a value, so Compose passes
the shell's token into the container when it is set and leaves it unset when it is
not. The run migrates the database and serves with bootstrap enabled, on loopback
inside the container, at `127.0.0.1:8000`.

Now create the first admin — **this step is not optional.** `athena-serve` refuses
to start a database that has no active administrator, so a volume that was
bootstrapped but never given one cannot be brought up again; you would have to
delete it and start over. CI performs exactly this sequence, which is how that
sharp edge was found.

The request has to be made from inside the container: the server is bound to the
container's loopback and `compose.yaml` publishes no port, so a `curl` on the host
cannot reach it, and the image has no `curl` either (its runtime stage installs
Python and the Athena wheel, nothing else). Its Python can, and the container
already holds the token. From a second terminal:

```bash
docker exec -it athena-bootstrap python -c '
import getpass, json, os, urllib.request
body = {"email": input("Admin email: "), "name": input("Admin name: ")}
body["password"] = getpass.getpass("Admin password: ")
if getpass.getpass("Confirm password: ") != body["password"]:
    raise SystemExit("passwords do not match; nothing was sent")
request = urllib.request.Request(
    "http://127.0.0.1:8000/users",
    data=json.dumps(body).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Athena-Bootstrap-Token": os.environ["ATHENA_BOOTSTRAP_TOKEN"],
    },
)
admin = json.load(urllib.request.urlopen(request, timeout=10))
print("created", admin["email"], "as", admin["role"])
'
```

The password is read at a prompt, never from a command argument. The request is
the one [OPERATIONS.md](OPERATIONS.md#first-user-bootstrap) describes: the token in
`X-Athena-Bootstrap-Token`, no `Idempotency-Key`, a nonblank password.

Then stop the bootstrap run (Ctrl-C in its terminal; `--rm` removes the container)
and bring the service up normally — **without** the token in the environment. A
token still set in the shell would be passed through, and `athena-serve` refuses to
start with one:

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

## Health

The image's `HEALTHCHECK` asks `/healthz`, over loopback inside the container: the
liveness check, which says the process is serving and does not open SQLite. It is
not the readiness check. `/readyz` opens the database and requires its migration
ledger to match the build ([OPERATIONS.md](OPERATIONS.md#health-checks)); ask it
from inside the container the same way:

```bash
docker compose exec athena python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=4).read().decode())'
```

A `docker compose ps` that says `healthy` therefore means "serving", not "serving
a database it can use"; a failing `/readyz` raises an HTTP error here.

## What lives in the volume

`/var/lib/athena` holds the SQLite database and the attachment blobs together, so a
backup is one path and is always internally consistent. Back it up with
`athena-backup` rather than by copying the file out from under a running process.
It takes the database to read and the file to write (the image sets
`ATHENA_DB=/var/lib/athena/athena.db`):

```bash
docker compose exec athena athena-backup /var/lib/athena/athena.db /var/lib/athena/backup.db
```

That snapshot is the database only, written into the same volume, and it refuses
to replace an existing `backup.db` unless you pass `--overwrite`. Copy it off the
volume before you rely on it. For a recovery point that includes the attachment
blobs, take a recovery bundle instead (`--recovery-bundle --attach-dir
/var/lib/athena/attachments`; see
[OPERATIONS.md](OPERATIONS.md#create-and-verify-a-matched-recovery-bundle)).

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

## No image is published — build it yourself

**Athena publishes no container image, by decision.** You build it locally with
`docker compose build`; CI builds the same `Dockerfile` on every push and proves it
(non-root, a fresh volume refused without an explicit bootstrap, the `HEALTHCHECK`,
a restart on an existing database), so the recipe is tested even though nothing is
pushed anywhere.

The reason is the maintenance promise, not the code: a published image has to be
rebuilt whenever its `python:3.12-slim` base takes a CVE, whether or not Athena
changed, and the image pulled in July is not the one published in March. That
promise pays off only once people are pulling it, and with no supported
public-network shape ([SECURITY.md](../SECURITY.md#transport-encryption-is-the-tailnets-job))
a published image would mostly be pulled to do what this page says not to. Revisit
when that transport decision changes or someone asks for the image by name; the
change is a job in `publish.yml` beside the PyPI one. Decided 2026-11-26
([`decisions/2026-11-26-owner-decisions.md`](decisions/2026-11-26-owner-decisions.md)).
