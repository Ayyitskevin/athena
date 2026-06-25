# Athena Operations

This is the operator runbook for a local or tailnet Athena deployment. Athena is
still local-alpha software, so public internet exposure should be a separate,
deliberate hardening decision.

## Runtime Configuration

Athena reads configuration from environment variables at process start.

| Variable | Default | Use |
|----------|---------|-----|
| `ATHENA_DB` | `athena.db` | SQLite database path. Use an absolute path for a long-running service. |
| `ATHENA_TRUST_ACTOR_HEADER` | `false` | Accept `X-Athena-Actor` as identity fallback. Use only on a trusted local/tailnet box, normally only for headless bootstrap. |
| `ATHENA_COOKIE_SECURE` | `false` | Adds the HTTPS-only `Secure` flag to browser cookies. Set to `1` when Athena is served over HTTPS. Leave off for plain local HTTP. |
| `ATHENA_SESSION_TTL_DAYS` | `14` | Browser session lifetime. |
| `ATHENA_MAX_REQUEST_BODY_BYTES` | `1048576` | Request body cap. Set to `0` only if a trusted reverse proxy enforces the limit. |

A typical local run:

```bash
ATHENA_DB=/var/lib/athena/athena.db \
uvicorn athena.main:app --host 127.0.0.1 --port 8000
```

A typical HTTPS reverse-proxy run:

```bash
ATHENA_DB=/var/lib/athena/athena.db \
ATHENA_COOKIE_SECURE=1 \
uvicorn athena.main:app --host 127.0.0.1 --port 8000
```

Do not set `ATHENA_COOKIE_SECURE=1` when serving plain HTTP directly; the browser
will refuse to send the login cookie back over HTTP.

## Health Checks

- `GET /healthz` is a cheap liveness check and does not touch SQLite.
- `GET /readyz` opens SQLite and verifies the schema is migrated.

Example:

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

Migrations run automatically during app startup. A failing `/readyz` usually
means the app cannot open the configured `ATHENA_DB` path or the schema did not
finish migrating.

## Backup and Restore

Use the packaged commands for SQLite snapshots:

```bash
athena-backup /var/lib/athena/athena.db /backups/athena-$(date +%F).db
```

`athena-backup` uses SQLite's online backup API, so Athena may stay running
while the backup is taken. It refuses to overwrite an existing backup unless
`--overwrite` is passed.

Restore while Athena is stopped:

```bash
athena-restore /backups/athena-YYYY-MM-DD.db /var/lib/athena/athena.db
```

`athena-restore` refuses to overwrite an existing database unless `--force` is
passed. When forcing a restore, it also removes stale `-wal` and `-shm` sidecar
files for the target database after replacing the main file.

## First User Bootstrap

The first user in an empty database can be created without authentication. Athena
always makes that first user an `admin` so the instance cannot start with no
administrator.

```bash
curl -fsS -X POST http://127.0.0.1:8000/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","name":"Admin","password":"change-me"}'
```

After the first user exists, creating users requires an admin actor. Browser
admins can create users, change roles, and set or reset browser passwords at
`/admin/users`.

## Roles

Athena has three user roles:

| Role | Behavior |
|------|----------|
| `admin` | Can manage users and perform normal writes. Admin is also required for user-admin API operations. |
| `member` | Can perform normal Aegis and Mentor writes and manage their own API tokens. |
| `viewer` | Can sign in and read, but cannot mutate state or manage tokens. |

The last admin cannot be demoted through either the web UI or REST API.

## Browser Password Management

Signed-in users can change their own browser password at `/settings/password`.
They must provide the current password and confirm the replacement password.

## Browser Token Management

Signed-in users can open `/settings/tokens` to inspect their API tokens. Members
and admins can create and revoke their own tokens. Viewers can see their token
metadata, but the create and revoke controls are hidden and server-side writes
are rejected.

A newly created raw token is shown once. Athena stores only a hash, so a lost raw
token must be revoked and replaced.

## Token Scopes

Token scopes narrow a bearer token below the user's role. They never expand a
user's role: a viewer with an `admin` token is still read-only.

| Scope | Allows |
|-------|--------|
| `read` | Authenticate and read/list permitted resources, including the owner's token metadata. |
| `issue:write` | Write Aegis issue/project/label/comment/status state. |
| `docs:write` | Write Mentor space/page/version state. |
| `admin` | Full token authority, including user-admin API operations and token mint/revoke. Also satisfies write scopes. |

Default and legacy tokens use `admin` scope for compatibility. If a token includes
`admin` with any other scope, Athena stores it as just `admin` because admin
already includes the others.

API token creation accepts an optional `scopes` list:

```bash
curl -fsS -X POST http://127.0.0.1:8000/tokens \
  -H "Authorization: Bearer $ATHENA_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"aegis-bot","scopes":["read","issue:write"]}'
```

Use the narrowest scope that matches the actor:

- A triage bot: `read`, `issue:write`
- A docs bot: `read`, `docs:write`
- An operator token for user/token administration: `admin`
- A dashboard or read-only integration: `read`

## Headless Admin Token Bootstrap

If you cannot use the browser UI to create the first admin token, temporarily
trust the local actor header, mint a token for user id `1`, then turn the header
back off.

1. Start Athena on a trusted local/tailnet interface with actor-header trust on:

   ```bash
   ATHENA_DB=/var/lib/athena/athena.db \
   ATHENA_TRUST_ACTOR_HEADER=1 \
   uvicorn athena.main:app --host 127.0.0.1 --port 8000
   ```

2. Mint an admin token for the bootstrap admin:

   ```bash
   curl -fsS -X POST http://127.0.0.1:8000/tokens \
     -H 'X-Athena-Actor: 1' \
     -H 'Content-Type: application/json' \
     -d '{"name":"bootstrap-admin","scopes":["admin"]}'
   ```

3. Store the returned `token` value somewhere safe. It is shown once.

4. Restart Athena without `ATHENA_TRUST_ACTOR_HEADER=1`.

Never expose an instance that trusts `X-Athena-Actor` to untrusted clients. If a
reverse proxy sits in front of Athena, strip inbound `X-Athena-Actor` unless the
proxy itself is intentionally providing that identity on a private network.

## Exposure Checklist

Before leaving laptop-only development:

- Set `ATHENA_DB` to an absolute path owned by the service user.
- Keep the bind address private: `127.0.0.1` behind a reverse proxy, or a tailnet
  address for tailnet-only use.
- Leave `ATHENA_TRUST_ACTOR_HEADER` unset except during headless bootstrap.
- Set `ATHENA_COOKIE_SECURE=1` when the browser reaches Athena over HTTPS.
- Keep `/readyz` in the service or reverse-proxy health check.
- Run `athena-backup` on the configured SQLite database path and store the
  snapshot somewhere outside the service host.
