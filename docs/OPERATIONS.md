# Athena Operations

This is the operator runbook for a local or tailnet Athena deployment. Athena is
still local-alpha software, so public internet exposure should be a separate,
deliberate hardening decision.

## Runtime Configuration

Athena reads configuration from environment variables at process start.

| Variable | Default | Use |
|----------|---------|-----|
| `ATHENA_DB` | `athena.db` | SQLite database path. Use an absolute path for a long-running service. |
| `ATHENA_LOG_LEVEL` | `INFO` | Level for Athena's own logs (`athena.*`). At `INFO` startup logs the migrations it applied and which background loops started, and the loops log any swallowed error. Set `WARNING` for a quieter server or `DEBUG` when diagnosing. |
| `ATHENA_TRUST_ACTOR_HEADER` | `false` | Accept `X-Athena-Actor` as identity fallback. Use only on a trusted local/tailnet box, normally only for headless bootstrap. |
| `ATHENA_COOKIE_SECURE` | `false` | Adds the HTTPS-only `Secure` flag to browser cookies. Set to `1` when Athena is served over HTTPS. Leave off for plain local HTTP. |
| `ATHENA_SESSION_TTL_DAYS` | `14` | Browser session lifetime. |
| `ATHENA_MAX_REQUEST_BODY_BYTES` | `1048576` | Request body cap. Set to `0` only if a trusted reverse proxy enforces the limit. Also caps attachment uploads (the whole request must fit), so raise it for larger files. |
| `ATHENA_IDEMPOTENCY_TTL_SECONDS` | `86400` | Retention for completed API idempotency receipts. Expired completed receipts are removed lazily. |
| `ATHENA_IDEMPOTENCY_LEASE_SECONDS` | `60` | Freshness window for an executing idempotency owner. Expiry never grants automatic takeover. |
| `ATHENA_IDEMPOTENCY_WAIT_SECONDS` | `5` | How long an identical concurrent retry waits for the owner to publish a result before receiving `409 idempotency_in_progress`. |
| `ATHENA_IDEMPOTENCY_MAX_RESPONSE_BYTES` | `1048576` | Largest successful response Athena will retain for safe replay. An overflow is fail-closed and becomes indeterminate. |
| `ATHENA_TOKEN_RATE_LIMIT_PER_MINUTE` | `120` | Per-bearer-token request ceiling for API/agent traffic. Set to `0` only when a trusted reverse proxy enforces equivalent token limits. |
| `ATHENA_ANON_RATE_LIMIT_PER_MINUTE` | `0` (off) | Per-client-IP ceiling on anonymous (credential-free) reads. Keyed by the direct peer IP, not `X-Forwarded-For`. Enable (e.g. `120`) wherever anonymous reads face an untrusted network; behind a proxy every anonymous request shares the proxy's IP, so account for it there instead. |
| `ATHENA_WEBHOOK_DELIVERY` | `true` | Run the in-process webhook delivery loop. Exactly one process per deployment may run it — see Background Loops. |
| `ATHENA_WEBHOOK_INTERVAL` | `5` | Seconds between webhook delivery passes. |
| `ATHENA_WEBHOOK_TIMEOUT` | `5` | Cap in seconds on each outbound webhook POST, so one slow receiver cannot stall the loop. |
| `ATHENA_AUTOMATION` | `true` | Run the in-process automation rules loop. Same single-runner rule as webhook delivery. |
| `ATHENA_AUTOMATION_INTERVAL` | `5` | Seconds between automation passes. |
| `ATHENA_ATTACH_DIR` | `attachments` | Directory for uploaded attachment blobs. Use an absolute path owned by the service user; keep it outside any web-served directory (files are only reachable via the authenticated download route). |
| `ATHENA_ATTACH_MAX_BYTES` | `10485760` | Per-attachment size cap (bounded by `ATHENA_MAX_REQUEST_BODY_BYTES`). |

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

## Background Loops (Webhooks and Automation)

Athena runs two in-process background loops: webhook delivery (pushes new
events to registered webhooks) and the automation rules engine (drains new
activity events and fires matching rules' actions). Both default on.

Each loop must run in exactly one process per deployment. If you run multiple
uvicorn workers, keep the loops enabled in one worker and start the others with
`ATHENA_WEBHOOK_DELIVERY=0` and `ATHENA_AUTOMATION=0` — otherwise webhooks
double-deliver and rules fire twice.

A rule's action runs best-effort (at-most-once): a failing action never wedges the
engine, but it is no longer silent. Each failure is logged at `WARNING` and recorded
on the rule (`failure_count`, `last_error`, `last_error_at`), shown as a red badge in
**Admin → Automation** and returned by `GET /automation/rules`, so a misbehaving rule
is visible rather than quietly dropping events.

## Single Sign-On (OIDC)

SSO is off unless all four connection settings are present; until then the SSO
routes 404 and the login page shows no SSO button. Local email+password login
keeps working either way — SSO is an additional way to authenticate.

| Variable | Use |
|----------|-----|
| `ATHENA_OIDC_ISSUER` | The IdP's issuer URL; its `/.well-known/openid-configuration` is discovered from it. |
| `ATHENA_OIDC_CLIENT_ID` | Client id registered with the IdP. |
| `ATHENA_OIDC_CLIENT_SECRET` | Client secret registered with the IdP. |
| `ATHENA_OIDC_REDIRECT_URL` | This app's callback URL. Must exactly match what the IdP has registered. |
| `ATHENA_OIDC_ALLOWED_DOMAINS` | Optional comma-separated email-domain allow-list for first-login auto-provisioning (e.g. `acme.com,acme.io`). Empty = any domain the IdP asserts. Set it to lock SSO to your organization. |

## Deploy Preflight

Run `athena-doctor` before switching a service to a restored or migrated database:

```bash
athena-doctor /var/lib/athena/athena.db \
  --attach-dir /var/lib/athena/attachments
```

The command opens the database the same way the app needs to use it, verifies
SQLite integrity, confirms every packaged migration has been applied, and checks
that the attachment directory exists and can accept writes. It does not apply
migrations unless `--migrate` is passed.

For first install or an intentional offline upgrade:

```bash
athena-doctor /var/lib/athena/athena.db --migrate \
  --attach-dir /var/lib/athena/attachments
```

Follow the preflight with the service-level `/readyz` check after startup.

## Backup and Restore

Use the packaged commands for SQLite snapshots:

```bash
athena-backup /var/lib/athena/athena.db /backups/athena-$(date +%F).db
```

`athena-backup` uses SQLite's online backup API, so Athena may stay running
while the backup is taken. It refuses to overwrite an existing backup unless
`--overwrite` is passed.

For local retention, keep the newest matching snapshots in the destination
directory:

```bash
athena-backup /var/lib/athena/athena.db /backups/athena-$(date +%F).db --keep 14
```

With `--keep`, the default retention glob is `<source-db-stem>-*.db`
(`athena-*.db` for `athena.db`). Pass `--retention-glob 'name-*.db'` for another
file-name pattern. Retention never walks directories; it only deletes older
matching sibling files after the new backup succeeds.

Restore while Athena is stopped:

```bash
athena-restore /backups/athena-YYYY-MM-DD.db /var/lib/athena/athena.db
```

`athena-restore` refuses to overwrite an existing database unless `--force` is
passed. When forcing a restore, it removes stale `-wal` and `-shm` sidecar
files for the target database before replacing the main file, so a reader can
never replay the old write-ahead log on top of the restored database.

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

## Activity Export

The browser activity feed at `/aegis/activity` includes a CSV download for the
current audit filters. The export uses the same actor, event, kind, target, and
search filters as the page and is capped to the newest 1000 matching rows.

Issue activity is authorized against both the issue's current project and every
project whose facts an event contains. This prevents moving an issue from
republishing private status, sprint, or project history. Events created before
the event-scope migration, and issue events replayed from a selective portability
bundle, do not carry trustworthy historical scope; they fail closed to the admin
forensic view. New native events capture scope automatically. A non-admin who can
read the current issue but not its entire scoped trail sees the permitted activity
subset, while exact time-travel returns 403 instead of reconstructing partial state.

Project, space, and page IDs that have appeared in live rows or activity are
reserved permanently. Deleting the live target never lets SQLite recycle its ID
for a new public target, so old private audit rows cannot be rebound by numeric-ID
reuse. Selective imports allocate from the same monotonic sequences.

## Run Replay Artifact Export

Use `athena-export-run` when you need to freeze one tagged run for audit, agent
handoff, or incident review without replaying side effects:

```bash
athena-export-run /var/lib/athena/athena.db goal-123 /exports/run-goal-123.json
```

The artifact includes the run's events in `activity.id ASC` order, the
run/fork coordinates, a light ancestor/descendant lineage tree, and the
determinism contract that marks which fields are replay-safe facts. It does not
execute handlers or mutate the database. Existing artifact files are not replaced
unless `--overwrite` is passed.

## Selective Portability Export

Use `athena-export` when you need a portable JSON bundle for one project or one
space without taking a whole-database snapshot:

```bash
athena-export /var/lib/athena/athena.db project 1 /exports/project-1.json
athena-export /var/lib/athena/athena.db space 1 /exports/space-1.json
```

The V1 bundle includes the selected container, its child issues or pages, page
versions where applicable, labels, links, membership rows, comments, activity
rows, and an attachment manifest. It does not include raw attachment blobs,
password hashes, API tokens, sessions, OIDC transient state, idempotency records,
or webhook secrets. Existing export files are not replaced unless `--overwrite`
is passed.

Use `athena-map-source` when you have a small source-system JSON export and want
to turn it into Athena's portability bundle format before any database write:

```bash
athena-map-source jira-project /imports/jira-issues.json /exports/jira-project.json
athena-map-source confluence-space /imports/confluence-pages.json /exports/confluence-space.json
athena-map-source jira-project /imports/jira-issues.json /exports/jira-project.json --report-path /exports/jira-project.report.json
```

The mapper is intentionally narrow. `jira-project` expects Jira issue-search style
JSON with an `issues` array. `confluence-space` expects Confluence content/search
style JSON with a `results` or `pages` array. The output is still only a bundle:
review it, then run the same dry-run, manifest, and import flow below. Users are
mapped by email, so create matching Athena users before dry-run. Missing source
emails become `@import.local` placeholders that dry-run will surface as missing
users. Raw attachment files are not imported by the mapper. Every mapped bundle
includes `source.mapping_report`; pass `--report-path` to write that report as a
standalone JSON file. Review `unmapped_fields` before import so custom Jira
fields, Confluence restrictions, external links, and source-only metadata are
handled deliberately instead of silently assumed.

Use `athena-import-dry-run` before planning a selective import into another
Athena database:

```bash
athena-import-dry-run /var/lib/athena/athena.db /exports/project-1.json
athena-import-dry-run /var/lib/athena/athena.db /exports/project-1.json --json
```

The dry-run is read-only. It validates the bundle schema, checks that referenced
users can be mapped by email in the target database, detects project name/key or
space key conflicts, reports labels that would be created or reused, summarizes
row counts, and warns about external links or attachment manifests that need a
replay plan. A blocked dry-run exits nonzero.

Use `athena-import-manifest` to write the replay plan that a future mutating
import should follow:

```bash
athena-import-manifest /var/lib/athena/athena.db /exports/project-1.json /exports/project-1.manifest.json
athena-import-manifest /var/lib/athena/athena.db /exports/project-1.json /exports/project-1.manifest.json --attachment-policy require-blobs
```

The manifest is also read-only against the target database. It embeds the dry-run
report, maps bundle user ids to target user ids by email, maps labels to existing
target ids or future create refs, assigns stable target refs for rows that would
be created, and lists replay operations in dependency order. The default
attachment policy is `skip`, because V1 bundles contain attachment metadata but
not raw blobs. `require-blobs` blocks manifests that contain attachment rows so an
operator cannot accidentally plan a lossy replay. Existing manifest files are not
replaced unless `--overwrite` is passed.

Use `athena-import` only after reviewing a ready manifest:

```bash
athena-import /var/lib/athena/athena.db /exports/project-1.json /exports/project-1.manifest.json
athena-import /var/lib/athena/athena.db /exports/project-1.json /exports/project-1.manifest.json --json
```

Replay takes a write lock, regenerates the manifest plan against the current
target database, and refuses to import if the reviewed manifest is blocked or
stale. It creates the selected project or space, child rows, membership, labels,
comments, issue dependencies, internal body backlinks, activity history, and FTS
search rows in one transaction. V1 still skips raw attachment blobs and external
cross-container links: the literal `[[issue:N]]` or `[[page:N]]` text remains in
the imported body, but backlinks are created only when both sides are part of the
same imported bundle. Regenerate the manifest after any target database change
that affects user, label, project, or space mapping.

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

Browser admins can use `/admin/agents` to review agent token posture. The page
flags agent accounts with no live token, live `admin`-scoped tokens, live tokens
that have never been used, and live tokens unused for 30+ days. These warnings are
read-only; they do not change authentication or token behavior.

Bearer-token API traffic is rate limited per token by
`ATHENA_TOKEN_RATE_LIMIT_PER_MINUTE`. A rejected request returns `429`, a
`Retry-After` header, and rate-limit headers. Browser sessions and the trusted
`X-Athena-Actor` bootstrap path are not token-limited; leave actor-header trust
off anywhere untrusted.

## Durable API Retry Keys

Authenticated REST mutations under Athena's API roots accept an
`Idempotency-Key` header on `POST`, `PUT`, `PATCH`, and `DELETE`. Use one
stable, unique key for one exact logical request and reuse it only when retrying
that request:

```bash
curl -fsS -X POST http://127.0.0.1:8000/issues \
  -H "Authorization: Bearer $ATHENA_AGENT_TOKEN" \
  -H 'Idempotency-Key: run-42-create-issue-7' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Investigate retry safety"}'
```

The key must be 1–255 visible ASCII characters. Athena fingerprints the method,
raw path and query, bounded raw request body, `Accept`, content type/encoding,
conditional headers, and Athena run-lineage headers. Reusing a key for different
request bytes returns `409`; it never replays the first success over a different
mutation. Multipart retries must preserve the raw boundary and bytes, not merely
the same logical fields.

The first worker commits an executing claim before it runs the route. An
identical concurrent request waits up to `ATHENA_IDEMPOTENCY_WAIT_SECONDS`.
When the owner succeeds without an intervening authorization change, Athena
stores the bounded response before sending its first byte. Waiters and later
retries receive the same status/body and safe response headers plus
`Idempotent-Replay: true`. Completed receipts survive process restarts and
expire after `ATHENA_IDEMPOTENCY_TTL_SECONDS`.

Receipts are also fenced by a conservative, global authorization revision.
Role/token-principal changes, visibility or membership changes, and ownership or
container-placement changes can revoke access. If that revision differs from the
one captured by a key, Athena purges the stored status, headers, and body, retains
a non-expiring safety marker, and returns `409` with
`code: idempotency_authorization_changed`. The global fence is deliberately
broad: even an unrelated authorization change can block an older receipt.
A keyed access-changing mutation may return its fresh success once and then be
intentionally non-replayable. Reconcile the known mutation before choosing a new
key; otherwise a new key can apply it again.

There is one deliberate fail-closed boundary. Route mutations and receipt
finalization currently use separate SQLite transactions. If a worker fails,
returns a server error, exceeds the replay bounds, or outlives its lease, Athena
cannot prove whether the mutation committed. It returns
`409` with `code: idempotency_indeterminate` and never automatically lets a
new worker take over. Inspect the domain row and activity trail before manually
reconciling that key; blindly deleting the receipt and retrying can duplicate a
committed mutation. A fresh executing owner that merely needs longer than the
lease may still finalize its own fenced result.

One-time-secret creation explicitly rejects keys instead of copying secrets into
the replay table: `POST /tokens`, `POST /webhooks`, and their browser admin
forms. Browser/session routes are otherwise outside the idempotency contract;
never rely on a key to replay cookies, CSRF state, or HTML redirects. Invalid or
revoked bearer credentials cannot read an existing receipt. Each valid, supported
keyed bearer mutation—including a replay—consumes one token-rate-limit unit;
requests rejected earlier for key/route/body/secret policy do not.
Anonymous first-user creation with a key is rejected rather than silently
ignoring its retry contract.

The optional MCP `AthenaClient` and all 18 mutation tools accept an optional
`idempotency_key`. For a retry-critical operation, choose 1-255 visible ASCII
characters as an opaque, non-secret key before the first attempt and reuse it
only with the exact same tool arguments, credential, and run lineage. Omitting
the field preserves ordinary keyless semantics: Athena deliberately does not
generate a key inside the tool call,
because a caller cannot recover that key if the MCP response itself is lost.
Token rotation also changes the receipt identity, so reconcile an uncertain call
before retrying it under a new credential.

`AthenaError` retains the HTTP status, method, path, server detail, machine code,
and raw `Retry-After` value for programmatic callers while preserving its existing
human-readable message. `idempotency_in_progress` may be retried after the stated
delay with the same key and exact request. `idempotency_indeterminate` and
`idempotency_authorization_changed` require reconciliation; `idempotency_mismatch`
means the key was reused with a different request. The client never automatically
retries or silently substitutes a new key.

Across MCP, the failure remains an `isError` tool result whose text contains
`ATHENA_ERROR_JSON=<object>`; that compact object carries the same fields so an
MCP caller can branch on the code without parsing human prose.

## Issue Optimistic Concurrency

Issue creation and singleton reads emit a strong, opaque `ETag`. Guard core issue
edits and placement changes by copying that value exactly into `If-Match` on
`PATCH /issues/{id}` or `PUT /issues/{id}/assignee`, `/project`, or `/sprint`.
The header is optional for backward compatibility, but agent read-modify-write
loops should send it.

A stale strong tag returns `412` with `code: precondition_failed` and the
current `ETag` response header; refetch, merge deliberately, and retry with that
fresh tag. Malformed and oversized conditions return `400 invalid_if_match` and
`431 if_match_too_large`. Authorization and ordinary payload validation run
before tag comparison, and comparison plus mutation share one SQLite write
transaction, so two writers cannot both commit from the same tag. Other issue
sub-resource mutations are not yet conditional.

The official client exposes a successful response tag as `_etag`; its four
guarded issue methods accept `if_match`, and `AthenaError.current_etag` carries
the 412 response tag. When combining `If-Match` with `idempotency_key`, changing
the precondition changes the request fingerprint: use a new idempotency key for
the merged retry.

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

## AI Agent Access (MCP)

Athena ships an optional **MCP server** so an AI agent can drive it through the
Model Context Protocol. It is a thin client over the REST API: every tool call
becomes an authenticated API call, so an agent acting over MCP is bound by the
same token scopes and leaves the same audit trail as any other client.

Install the extra and run it with a scoped token for a running Athena:

```bash
pip install -e ".[mcp]"

ATHENA_BASE_URL=http://127.0.0.1:8000 \
ATHENA_TOKEN=ath_... \
athena-mcp
```

The server speaks MCP over stdio (how desktop/agent clients launch tool servers).
Point your MCP client at the `athena-mcp` command with those two environment
variables. Give the agent the **narrowest token** that fits its job (e.g. a triage
bot gets `read`, `issue:write`); the MCP server never widens what the token allows.

It exposes tools for searching, reading and writing issues (create/update/assign/
comment), reading and writing Mentor pages, listing projects/users/spaces, and
reading the event feed. The `mcp` extra is kept out of the base install, so the
core app and its tests do not depend on it.

Every mutation tool exposes the optional `idempotency_key` field. A caller that
may retry must create and retain a stable key before its first invocation; use the
same key only for an exact replay, and never place credentials or user content in
it. The MCP schema enforces the 1-255 visible-ASCII bound before dispatch. Read
tools do not accept keys. The four guarded issue mutation tools also accept
`if_match`; call `get_issue`, copy its `_etag` exactly, and follow the 412 merge
procedure above.

## Exposure Checklist

Before leaving laptop-only development:

- Set `ATHENA_DB` to an absolute path owned by the service user.
- Keep the bind address private: `127.0.0.1` behind a reverse proxy, or a tailnet
  address for tailnet-only use.
- Leave `ATHENA_TRUST_ACTOR_HEADER` unset except during headless bootstrap.
- Set `ATHENA_COOKIE_SECURE=1` when the browser reaches Athena over HTTPS.
- Set `ATHENA_ANON_RATE_LIMIT_PER_MINUTE` (e.g. `120`) if anonymous reads are
  reachable from an untrusted network.
- If you run multiple worker processes, disable the webhook and automation
  loops in all but one (see Background Loops).
- Keep `/readyz` in the service or reverse-proxy health check.
- Run `athena-doctor` against the configured database and attachment directory
  before exposing a restored, moved, or upgraded instance.
- Run `athena-backup` on the configured SQLite database path and store the
  snapshot somewhere outside the service host.
