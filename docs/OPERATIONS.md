# Athena Operations

This is the operator runbook for a local or tailnet Athena deployment. Athena is
still local-alpha software. The checks in this runbook improve failure detection;
they do not make Athena ready for direct public-internet exposure.

## Supported Runtime and Deployment Shape

Athena currently supports Python 3.12 only: package metadata requires
`>=3.12,<3.13`, and CI runs on Python 3.12. Do not deploy it under Python 3.11,
3.13, or an untested alternate interpreter.

The supported deployment is one Athena application process with one uvicorn
worker, one SQLite database on local storage, and one attachment directory on the
same host. Access is expected from that trusted machine or a tailnet, optionally
through an HTTPS reverse proxy. Athena does not currently claim public-internet,
hostile multi-tenant, multi-process, or high-availability safety.

Exactly one process may own the webhook-delivery and automation runners. There is
no leader election between processes, and request/login/token rate limiters are
in-process. A multi-worker experiment must nominate one runner and disable both
loops everywhere else, but that remains outside the supported deployment shape.

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
| `ATHENA_AGENT_RUN_STALE_SECONDS` | `90` | Maximum check-in age still labeled `reporting_recently`; older reports are `stale`. |
| `ATHENA_AGENT_RUN_MAX_CHECKINS_PER_AGENT` | `1000` | Durable check-in row ceiling per agent. Existing ids remain refreshable at the ceiling; new ids receive `409`. |
| `ATHENA_ANON_RATE_LIMIT_PER_MINUTE` | `0` (off) | Per-client-IP ceiling on anonymous (credential-free) reads. Keyed by the direct peer IP, not `X-Forwarded-For`. Enable (e.g. `120`) wherever anonymous reads face an untrusted network; behind a proxy every anonymous request shares the proxy's IP, so account for it there instead. |
| `ATHENA_LOGIN_RATE_LIMIT_PER_MINUTE` | `10` | Per-client-IP cap on `POST /login` attempts, checked before the password hash (bounds brute force and pbkdf2 CPU). Over the limit returns `429` with `Retry-After`. Keyed by the direct peer IP; behind a shared-IP proxy, raise it or enforce at the proxy. Set `0` to disable. |
| `ATHENA_WEBHOOK_DELIVERY` | `true` | Run the in-process webhook delivery loop. Exactly one process per deployment may run it — see Background Loops. |
| `ATHENA_WEBHOOK_INTERVAL` | `5` | Seconds between webhook delivery passes. |
| `ATHENA_WEBHOOK_TIMEOUT` | `5` | Cap in seconds on each outbound webhook POST, so one slow receiver cannot stall the loop. |
| `ATHENA_AUTOMATION` | `true` | Run the in-process automation rules loop. Same single-runner rule as webhook delivery. |
| `ATHENA_AUTOMATION_INTERVAL` | `5` | Seconds between automation passes. |
| `ATHENA_ATTACH_DIR` | `attachments` | Directory for uploaded attachment blobs. Use an absolute path owned by the service user; keep it outside any web-served directory (files are reachable only through the target-visibility-gated download route). |
| `ATHENA_ATTACH_MAX_BYTES` | `10485760` | Per-attachment size cap (bounded by `ATHENA_MAX_REQUEST_BODY_BYTES`). |

Typed configuration fails closed at import/startup. Boolean settings accept only
`1`, `true`, `yes`, `on`, `0`, `false`, `no`, or `off` (case-insensitive).
Integer and floating-point settings must parse, meet their documented minimum,
and, for floats, be finite. `ATHENA_LOG_LEVEL` must be exactly one of
`CRITICAL`, `ERROR`, `WARNING`, `INFO`, or `DEBUG` after case normalization. A
typo or out-of-range value aborts startup instead of silently selecting a default.

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
- `GET /readyz` opens SQLite and requires the database migration ledger to match
  the exact packaged migration inventory, with no missing, unknown, future, or
  unpackaged entry and with every applied migration checksum matching its packaged
  file.

Example:

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

Migrations run automatically during app startup. A failing `/readyz` means the
app cannot safely use the configured `ATHENA_DB` path or its migration ledger does
not match this build. `/readyz` does not run SQLite's full integrity check and does
not inspect attachment blobs; use `athena-doctor` for those checks.

## Background Loops (Webhooks and Automation)

Athena runs two in-process background loops: webhook delivery (pushes new
events to registered webhooks) and the automation rules engine (drains new
activity events, claims bounded UTC schedule slots, and fires matching rules'
actions). Both default on. Scheduled-rule configuration and recovery semantics are
documented in [AUTOMATION_SCHEDULES.md](AUTOMATION_SCHEDULES.md).

Each loop must run in exactly one process per deployment. Athena's supported shape
is one process and one worker. If an operator deliberately starts multiple
independent processes, exactly one may retain the default loop settings and every
other process must start with `ATHENA_WEBHOOK_DELIVERY=0` and
`ATHENA_AUTOMATION=0`; otherwise webhooks double-deliver and rules fire twice.
That arrangement remains an operator-managed experiment, not a supported HA mode.

For maintenance, first remove or drain inbound traffic, then ask the process
manager for a normal graceful stop and wait for the Athena process to exit. The
FastAPI shutdown path cancels both background tasks together and awaits them.
Only after the process has exited should you replace the database or attachment
directory. Do not use an abrupt kill as the normal backup/restore procedure.

Event-rule actions remain best-effort and at-most-once. Scheduled target actions use
durable per-target receipts with at most three attempts and action-run deduplication;
catch-up and target/action budgets prevent unbounded restart bursts. A failing action
never wedges the engine, but it is no longer silent. Each failure is logged at `WARNING`
and recorded on the rule (`failure_count`, `last_error`, `last_error_at`), shown as a red badge in
**Admin → Automation** and returned by `GET /automation/rules`, so a misbehaving rule
is visible rather than quietly dropping events.

## Single Sign-On (OIDC)

SSO is off only when all four connection settings are unset; in that state the
SSO routes 404 and the login page shows no SSO button. Supplying only some of the
four settings is rejected at startup with the missing variable names. Supplying
all four enables SSO. Local email+password login remains available — SSO is an
additional authentication path.

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

The command runs SQLite's full `PRAGMA integrity_check` and requires the applied
migration ledger to match the exact packaged inventory and checksums. With
`--attach-dir`, it also checks that the directory exists, is writable, and
reconciles its direct entries against the selected database. It fails on missing,
tampered, size-mismatched, unreadable, or non-regular blobs; orphan files; and an
unsafe storage root. Symlinks, FIFOs, devices, sockets, and directories are not
followed or hashed. Findings are reported as category counts, not blob names or
content.

`athena-doctor` detects but does not repair attachment divergence. Omitting
`--attach-dir` omits all attachment checks. It does not apply migrations unless
`--migrate` is passed.

For first install or an intentional offline upgrade:

```bash
athena-doctor /var/lib/athena/athena.db --migrate \
  --attach-dir /var/lib/athena/attachments
```

Follow the preflight with the service-level `/readyz` check after startup.

## Attachment Storage Integrity

New uploads are written to a private, unique sibling staging file, flushed and
fsynced, then atomically replaced into their random server-owned name. The
containing directory is fsynced before the metadata-and-activity transaction
commits, so a reader that can resolve the committed row never receives a partially
written blob. Audit, notification, run-binding, insert, write, publication, and
commit failures roll back SQLite state and synchronously attempt to remove the new
blob. If that rollback cleanup also fails, both errors surface together and
reconciliation can detect the residual orphan. Downloads open the
server-generated name through a descriptor-anchored, no-follow regular-file check
rather than a pathname check followed by a second open.

SQLite and the filesystem are still separate durability domains. An abrupt
process or host failure in the narrow publication/commit window can leave an
orphan file, and external filesystem damage can leave a missing, size-mismatched,
or checksum-mismatched row. The deterministic reconciliation used by
`athena-doctor --attach-dir` reports those states and fails closed on symlinks and
other non-regular entries instead of following or hashing them.

Attachment deletion commits the metadata removal and its activity/notification
facts together before the filesystem unlink. If unlink or directory sync fails, the
operation raises rather than silently claiming complete cleanup; the audit event still
records the committed removal. An unlink failure can leave an orphan for doctor to
detect; a directory-sync failure after removal still raises because durability is
uncertain. Hard page deletion attempts blob, outgoing-link, and search cleanup
independently and reports every failure. Do not manually delete or merge files solely
from a category count.
Keep the service stopped, preserve a recovery copy, and reconcile the database
with the intended attachment snapshot first.

## Backup and Restore

`athena-backup` snapshots only the SQLite database. It does **not** include any
blob under `ATHENA_ATTACH_DIR`. A database-only snapshot can be taken online:

```bash
athena-backup /var/lib/athena/athena.db /backups/athena-$(date -u +%F).db
```

`athena-backup` uses SQLite's online backup API, so Athena may stay running
while that database-only backup is taken. It refuses to overwrite an existing
backup unless `--overwrite` is passed. Such a snapshot is not a complete recovery
point for an instance that has attachments.

For local retention, keep the newest matching snapshots in the destination
directory:

```bash
athena-backup /var/lib/athena/athena.db /backups/athena-$(date +%F).db --keep 14
```

With `--keep`, the default retention glob is `<source-db-stem>-*.db`
(`athena-*.db` for `athena.db`). Pass `--retention-glob 'name-*.db'` for another
file-name pattern. Retention never walks directories; it only deletes older
matching sibling files after the new backup succeeds.

### Create a complete recovery pair

A complete recovery point is one SQLite snapshot plus the matching attachment
directory snapshot. To prevent uploads or deletes between the two, drain traffic,
gracefully stop Athena, and wait for its process and background runners to exit.
Then capture both under one identifier. For example, with GNU `tar`:

```bash
SNAPSHOT_ID="$(date -u +%Y%m%dT%H%M%SZ)"
SNAPSHOT_DIR="/backups/athena-${SNAPSHOT_ID}"
mkdir -m 0700 -- "$SNAPSHOT_DIR"
athena-backup /var/lib/athena/athena.db "$SNAPSHOT_DIR/athena.db"
tar --create --file "$SNAPSHOT_DIR/attachments.tar" \
  --directory /var/lib/athena attachments
```

An equivalent filesystem snapshot is fine if it preserves the directory tree,
random stored names, and file bytes. Keep the pair together, record its identifier,
and store a copy away from the service host. Restart Athena only after the pair is
complete.

### Restore a matched pair

Restoration is an offline operation. Drain traffic, gracefully stop Athena, and
wait for the process to exit. Before changing either target, create a separate
pre-restore recovery pair of the current database and attachment directory. Keep
that pair until the restored instance has passed operator acceptance.

Select the exact matched snapshot directory first. Set `BACKUP_DIR` in the
operator shell to that directory; each snippet below refuses to run if it is unset.
Extract the candidate attachment archive into a new private sibling directory;
do not merge it into the live directory:

```bash
: "${BACKUP_DIR:?set BACKUP_DIR to the selected matched snapshot directory}"
RESTORE_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RESTORE_STAGE="/var/lib/athena/attachments.restore-${RESTORE_ID}"
mkdir -m 0700 -- "$RESTORE_STAGE"
tar --extract --file "$BACKUP_DIR/attachments.tar" \
  --directory "$RESTORE_STAGE" --strip-components=1
```

Restore the matching database while Athena remains stopped. An existing target
requires `--force`:

```bash
: "${BACKUP_DIR:?set BACKUP_DIR to the selected matched snapshot directory}"
athena-restore "$BACKUP_DIR/athena.db" /var/lib/athena/athena.db --force
```

`athena-restore` first copies the candidate into a private sibling stage, runs
SQLite `quick_check`, and fsyncs it before touching the target. For a forced
restore it also takes a consistent recovery snapshot of the current target and
directory-syncs that recovery name before destructive work. It then removes stale
`-wal` and `-shm` sidecars, atomically replaces the database, and fsyncs the parent
directory. If sidecar cleanup or replacement fails, it automatically restores that
recovery snapshot. The command still exits with the original failure, so do not
proceed merely because automatic rollback succeeded.

Before swapping attachment directories, reconcile the restored database against
the candidate stage:

```bash
athena-doctor /var/lib/athena/athena.db --attach-dir "$RESTORE_STAGE"
```

If doctor fails, leave the service stopped and restore the explicit pre-restore
recovery pair. If it passes, rename the old directory aside and move the staged
directory into place on the same filesystem:

```bash
CURRENT_ATTACH_DIR=/var/lib/athena/attachments
PREVIOUS_ATTACH_DIR="/var/lib/athena/attachments.pre-restore-${RESTORE_ID}"
mv -- "$CURRENT_ATTACH_DIR" "$PREVIOUS_ATTACH_DIR"
mv -- "$RESTORE_STAGE" "$CURRENT_ATTACH_DIR"
athena-doctor /var/lib/athena/athena.db --attach-dir "$CURRENT_ATTACH_DIR"
```

Keep `PREVIOUS_ATTACH_DIR` and the pre-restore database snapshot until acceptance.
After the final doctor passes, start the single Athena process, wait for startup,
and then check readiness before restoring traffic:

```bash
curl -fsS http://127.0.0.1:8000/readyz
```

### Recover from an interrupted restore

Do not start Athena while the database/attachment pair is uncertain. Preserve all
candidate, pre-restore, and dot-prefixed recovery files; do not choose one by
mtime and do not merge attachment trees.

- If `athena-restore` reports an ordinary sidecar-cleanup or replacement failure,
  it attempted to restore its consistent recovery snapshot. Keep the service stopped and run
  doctor against the intended original pair before doing anything else.
- If it says automatic recovery also failed, the error names the consistent
  recovery copy that was deliberately retained. Do not move or delete it. Prefer
  restoring the explicit pre-restore database snapshot, then reconcile it against
  the matching pre-restore attachment directory.
- If interruption happened after the database restore but before the attachment
  swap, either complete the staged-pair workflow after doctor passes or roll the
  database back to the explicit pre-restore snapshot. Never start with the new
  database and old attachments by accident.
- If interruption happened between the two attachment renames, the explicit
  `attachments.pre-restore-*` and `attachments.restore-*` names show both sides.
  Move the old directory back or finish the candidate move only after selecting
  the matching database and passing doctor.

The recovery gate remains: stopped service, one known matched pair, doctor against
the final paths, normal startup, then `/readyz`. A successful check supports this
local/tailnet recovery procedure; it is not a claim of public deployment readiness.

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
cooperative agent-run check-ins, operational claim handoffs, or webhook secrets.
Imported activity rows never synthesize actionable handoffs. Use a full SQLite
backup/restore when lease-era operational state must be preserved. Existing export
files are not replaced unless `--overwrite` is passed.

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


## Delegation Pickup

An authenticated actor with a `read`-scoped token can pull the issues where it is
currently a contributor:

```bash
curl -fsS 'http://127.0.0.1:8000/delegations/me?limit=50&offset=0' \
  -H "Authorization: Bearer $ATHENA_AGENT_TOKEN"
```

MCP clients use `list_my_delegated_work`, which is a thin client over the same
REST route. The response includes the full issue description, accountable assignee,
labels, delegation actor and time, and a bounded preview of open blockers visible to
that same actor. It never reveals another contributor's inbox or a hidden blocker.

By default, the inbox excludes archived issues and every status in the owning
project's `done` category. Set `include_closed=true` to include done-category
work; archived issues remain excluded. `limit` defaults to 50 and is capped at
100; `offset` is bounded to SQLite's signed 64-bit range. Use `has_more` and
`next_offset` for paging. Results are deterministic:
urgent before high, medium, and low; within a priority, the oldest delegation appears
first.

This is pickup context, not a claim, lease, progress report, or liveness signal. The
response names its blocker scope as `visible_to_subject`; an empty blocker list
does not prove that no hidden blocker exists. The browser dashboard shows the same
self-service projection. Admins see a bounded open-delegation projection for every
agent at `/admin/agents`, including an explicit warning when an agent was added to
an issue in a private project it cannot access.

## Claiming and yielding delegated work

Before acquiring or renewing a lease, fetch `GET /issues/{id}` and copy its
response `ETag` header (exposed as `_etag` by the official client), or fetch Agent
Work Context and copy its JSON `issue_etag`. Do not use the context packet's
top-level `_etag`. Pass the root tag, including its quotes, as the only `If-Match`
field value:

```bash
curl -fsS -X POST http://127.0.0.1:8000/issues/42/claim \
  -H "Authorization: Bearer $ATHENA_AGENT_TOKEN" \
  -H "If-Match: $ATHENA_ISSUE_ETAG" \
  -H 'Idempotency-Key: goal-123-claim-42' \
  -H 'X-Athena-Run: goal-123' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

A free or expired acquisition omits `generation` as above and receives a fresh
opaque generation in the `201` lease body. An active same-holder renewal sends that
exact value in the JSON body; the generation remains stable for the possession:

```json
{"generation":"0123456789abcdef0123456789abcdef"}
```

Supplying `generation` always selects renewal mode, so a delayed request can never
silently acquire a later possession. The successful body also includes
`open_claim_handoff` when continuation context awaits acknowledgment. Claim and
lease-read responses are private and non-cacheable and carry no response `ETag`;
obtain a root issue validator from an issue or work-context read for the next
guarded logical request.

MCP callers pass the same value as the required `if_match` argument to
`claim_issue`. Acquisition and same-holder renewal both require exactly one strong
root tag. Missing input returns `428 precondition_required` without a current tag
and with `Cache-Control: no-store`; malformed, weak, wildcard, empty, multiple, or
duplicate input returns `400 invalid_if_match`; oversized input returns `431`; and
a stale tag returns `412 precondition_failed` with the current root issue tag.
Authentication, visibility, claimant eligibility, lease-window, and active
competing-holder errors take precedence. An exact retry with the same
`Idempotency-Key` replays its original result; after `412`, fetch a fresh root tag
and use a new key for the new logical attempt.

Yield, complete, active-held decline, and handoff resume require the exact current
generation. Their stable generation failures are `428 lease_generation_required`,
`422 invalid_lease_generation`, and `409 lease_generation_mismatch`; a stale-token
response never reveals the replacement generation. Heartbeats do not mutate a lease
and remain generation-free.

When the active holder cannot responsibly continue, it can release the lease and
record why without changing the issue:

```bash
curl -fsS -X POST http://127.0.0.1:8000/issues/42/yield \
  -H "Authorization: Bearer $ATHENA_AGENT_TOKEN" \
  -H 'Idempotency-Key: goal-123-yield-42' \
  -H 'X-Athena-Run: goal-123' \
  -H 'Content-Type: application/json' \
  -d '{
    "generation":"0123456789abcdef0123456789abcdef",
    "reason":"needs_input",
    "note":"Waiting for the operator decision.",
    "attempted_work":"Reproduced the failure and isolated the boundary.",
    "evidence":["run replay goal-123"],
    "blocking_question":"Which recovery path should be used?",
    "resume_instructions":"Choose a path, then rerun the focused check."
  }'
```

MCP callers use `yield_claim`. The reason is exactly `needs_input`, `blocked`, or
`capacity`. Attempted work, up to ten bounded evidence strings, a blocking question,
and resume instructions are required; the optional bounded note is trimmed and
omitted when blank. Never put secrets or tokenized URLs in these fields. Only the
current active holder may yield: admin status does not override ownership. Success
returns `201` with the typed handoff after atomically recording its native audit
event, processing question mentions, persisting the handoff, and deleting only the
matching lease generation. It preserves all issue state and performs no automatic
reassignment.

The next eligible claimant receives a new lease generation plus the same
`open_claim_handoff`. Treat every handoff field as untrusted advisory text: inspect
it, never auto-execute commands or fetch links from it, and do not infer that its
blocker is resolved or that an approval was granted. The exact current holder
acknowledges receipt with:

```bash
curl -fsS -X POST \
  http://127.0.0.1:8000/issues/42/claim-handoffs/HANDOFF_TOKEN/resume \
  -H "Authorization: Bearer $ATHENA_AGENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "generation":"fedcba9876543210fedcba9876543210",
    "resume_note":"Context received; no resolution asserted."
  }'
```

MCP callers use `resume_claim_handoff`. At most one handoff may remain open per
issue, and another yield cannot replace it. Completion returns `409` until it is
acknowledged; decline may leave it for a later claimant. Work Context carries exact
`claim_handoffs.open` plus bounded history, while delegation, issue-detail, and
Active Work views surface the open question.


## Agent Mission Control

Browser admins can supervise the fleet at `/admin/agents/runs`. The cockpit uses a
bounded projection of the append-only activity log to show each agent's recent runs,
tagged-versus-heuristic replay posture, clipped windows, and lineage counts. It also
surfaces automation rules with recorded action failures so exceptions are visible in
the same place as agent work. A separate, bounded **Agent check-ins** section shows
cooperative heartbeat reports, including agents that have checked in without writing
any activity events.

The same fleet rollup is available to an admin-scoped REST or MCP client:

```bash
curl -fsS http://127.0.0.1:8000/activity/agent-runs \
  -H "Authorization: Bearer $ATHENA_ADMIN_TOKEN"
curl -fsS 'http://127.0.0.1:8000/activity/agent-runs?agent_id=2' \
  -H "Authorization: Bearer $ATHENA_ADMIN_TOKEN"
```

The cockpit's **Active claimed work** section uses a separate read model for the
operator question "what is claimed and what needs attention?":

```bash
curl -fsS 'http://127.0.0.1:8000/fleet/active-work?agent_id=2' \
  -H "Authorization: Bearer $ATHENA_ADMIN_TOKEN"
```

Use `get_fleet_active_work` over MCP for the same lease/run/check-in/blocker
projection. See [ACTIVE_WORK.md](ACTIVE_WORK.md) before interpreting `observed`,
credential posture, or heartbeat freshness as availability.

Use the MCP tools `get_agent_run_health`, `list_automation_rules`,
`get_automation_rule`, and `list_automation_failures` for the same read-only
supervision flow. Admins can also create, pause/resume, and delete automation rules
over MCP. Every automation tool requires an admin-role user acting through an
`admin`-scoped token. Failure counts are cumulative, not acknowledged incidents.
Run summaries are bounded recent history: an event proves that an agent acted, not
that its external process is still running.

Heartbeat headline state is agent-based: `latest_checkins` contains exactly one
newest report per agent, selected from that agent's full retained check-in history.
The `latest_*` totals summarize those rows. `checkins` remains a bounded recent
history window, and the unsuffixed check-in totals continue to summarize that window
for compatibility. A stale historical run therefore does not make a currently
reporting agent look stale; parallel and older run ids remain inspectable in history.

An agent can cooperatively report that it is still working under a client-chosen
run identifier:

```bash
curl -fsS -X PUT http://127.0.0.1:8000/agent-runs/heartbeat \
  -H "Authorization: Bearer $ATHENA_AGENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"goal-123"}'
```

The caller must use bearer-token authentication, its user must be marked as an
agent, and the token must carry at least one write scope (`issue:write`,
`docs:write`, or `admin`). The body is intentionally strict: clients send only the
run id, never an actor id or timestamp. MCP clients use
`heartbeat_agent_run(run_id)` under the same authentication and authorization
rules.

Athena records `first_seen_at` once and refreshes the server-owned `last_seen_at`
on every accepted PUT. Repeated PUTs are therefore intentional refreshes, not
durable-idempotency replays; do not attach an `Idempotency-Key`. A heartbeat is
`reporting_recently` until its age crosses the server-time threshold configured by
`ATHENA_AGENT_RUN_STALE_SECONDS` (90 seconds by default), then it is `stale`.
Client clock values cannot extend that window.

To bound durable operational state, each agent may create at most
`ATHENA_AGENT_RUN_MAX_CHECKINS_PER_AGENT` distinct check-in rows (1,000 by
default). Reaching the ceiling returns `409` for a new run id; refreshes of an
existing id continue to work. Use stable run identifiers rather than minting a
new id for every heartbeat.

Check-ins are cooperative status only. They do not prove an OS process is alive,
do not add activity-log events (avoiding heartbeat log spam), and do not
automatically finish a run, revoke a token, transfer or take over work, or trigger
any other lifecycle action. Operators must reconcile stale reports with the agent
and the underlying work before acting.

The check-in panel separates latest-per-agent headlines from bounded recent
history. The explicit `latest_*` totals use full retained history, while
`checkins` and the unsuffixed compatibility totals describe only the bounded
history rows. A heartbeat-only identifier remains an operational sidecar; it does
not become a replayable activity run unless activity events are later written with
that run id.

## Durable API Retry Keys

Authenticated REST mutations under Athena's durable-idempotency API roots accept an
`Idempotency-Key` header on `POST`, `PUT`, `PATCH`, and `DELETE`. Cooperative
`PUT /agent-runs/heartbeat` is the explicit exception described above: a key is
rejected because every call must refresh server-owned time. For supported writes,
use one stable, unique key for one exact logical request and reuse it only when
retrying that request:

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
reading the event feed. Admin-scoped tools also expose fleet run health plus complete
event/schedule automation-rule lifecycle management and recorded failure health. The
`mcp` extra is kept out of the base install, so the core app and its tests do not depend
on it.

Every mutation tool exposes the optional `idempotency_key` field. A caller that
may retry must create and retain a stable key before its first invocation; use the
same key only for an exact replay, and never place credentials or user content in
it. The MCP schema enforces the 1-255 visible-ASCII bound before dispatch. Read
tools do not accept keys. The four guarded issue mutation tools also accept
`if_match`; call `get_issue`, copy its `_etag` exactly, and follow the 412 merge
procedure above.

## Exposure Checklist

Before leaving laptop-only development:

- Use Python 3.12 and one Athena process/uvicorn worker.
- Set `ATHENA_DB` to an absolute path owned by the service user.
- Set `ATHENA_ATTACH_DIR` to an absolute local-storage path owned by the service
  user and keep it outside every statically served directory.
- Keep the bind address private: `127.0.0.1` behind a reverse proxy, or a tailnet
  address for tailnet-only use.
- Leave `ATHENA_TRUST_ACTOR_HEADER` unset except during headless bootstrap.
- Set `ATHENA_COOKIE_SECURE=1` when the browser reaches Athena over HTTPS.
- Set `ATHENA_ANON_RATE_LIMIT_PER_MINUTE` (e.g. `120`) if anonymous reads are
  reachable from an untrusted network.
- Keep exactly one webhook/automation runner; the supported shape is the single
  process above (see Background Loops).
- Keep `/readyz` in the service or reverse-proxy health check.
- Run `athena-doctor` against the configured database and attachment directory
  before returning local/tailnet traffic to a restored, moved, or upgraded
  instance.
- Store a matched SQLite snapshot and attachment-directory snapshot away from the
  service host; `athena-backup` alone does not include attachment blobs.
- Do not treat this checklist as approval for direct public-internet exposure.
