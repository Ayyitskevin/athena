# Athena — Architecture

The design of record. This document explains *what* we're building and *why*,
so anyone (human or agent) joining later can get oriented fast.

## What Athena is

A **self-hosted operator workspace** for solo operators and their AI fleet —
the shape of Notion (docs + tasks in one place), the deployment model of
self-hosting (one SQLite file, your machine), and an agent layer neither Notion
nor Obsidian offers natively.

Two modules over one shared core:

| Module | Role | Holds |
|--------|------|-------|
| **Mentor** | Knowledge (Notion/Obsidian-adjacent) | spaces, pages (a tree), markdown bodies, versions, wikilinks |
| **Aegis** | Work (task tracker) | issues, statuses, boards, sprints/cycles, comments, labels |

We are **not** building a Notion block editor or an Obsidian vault-on-disk. We
**are** building a unified command center: write docs, track work, cross-link
both, and let agents do the same through a scoped, audited API.

The reason to build Mentor and Aegis together (not as two apps) is the
**cross-link**: an issue can reference a page and vice-versa, because they share
one database. That is the whole value of running notes and tasks in one workspace
— without paying for three SaaS products and bolting AI on afterward.

## Principles

1. **One integrated service.** Shared auth, DB, search, and cross-link resolver
   in a `core/` package; `aegis/` and `mentor/` are feature modules on top.
2. **API-first.** Durable capabilities have a REST surface. The web UI (Jinja +
   HTMX) is a thin transport adapter over the same domain and command owners,
   while MCP uses REST — so the AI fleet (Claude, Grok, Codex, Hermes) acts with
   scoped, audited identity instead of through a second data owner. "Grok closed
   AEGIS-88" is first-class history, not a mystery. Command-backed writes
   converge on one framework-neutral application command; that command commits
   the state change, derived links/search rows, notifications, and audit event
   atomically, so transports cannot drift or leave unaudited state behind.
   Command migration remains incremental; the current migrated inventory and
   known debt live in [`COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md), not in a
   frozen example here.
3. **Local-first, no premature hosting.** Built and version-controlled as a
   normal software project. Self-hosting on a dedicated home-lab node is a
   *later* decision, not a starting assumption.
4. **Greenfield until cutover.** While we build, Athena reads from and writes to
   nothing else. The operator's existing systems (a wikilink markdown vault for
   docs, a hosted tracker for tasks) stay the source of truth. At the end we run
   a one-time **migration**, not an ongoing sync — that avoids the "two systems,
   which one is right?" trap.

## Stack

- **Python 3.12** (exact supported range `>=3.12,<3.13`), **FastAPI** (web
  framework), **Jinja2** (HTML templating), **HTMX** (interactivity without a
  JS build step).
- **SQLite** in WAL mode — one file, trivially backed up, plenty for one
  operator. Full-text search via SQLite's built-in **FTS5**.
- Filesystem store for attachments. No external services required to run.

## Layout

```
src/athena/
  core/      auth, users, agent tokens, db + migrations, search (FTS5),
             attachments, activity log, cooperative run check-ins, notifications,
             webhooks, backups, portability (export/import), OIDC, the issue<->doc
             cross-link resolver
  aegis/     issues, projects, statuses, boards, sprints, labels, comments,
             saved filters, automation rules, and application commands that own
             audited write transactions shared by REST + web
  mentor/    spaces, pages (tree), versions, page comments
  web/       route handlers for the browser UI (Jinja + HTMX) — a thin client
             over the same data the REST API serves, never a second data owner
  mcp/       optional MCP server — a thin client over the REST API for agents
  config.py  env-driven settings (ATHENA_DB, ...)
  main.py    app factory: create_app(), middleware, lifespan, router wiring,
             /healthz liveness, /readyz readiness
  ops.py     operator CLIs (athena-backup, athena-doctor, athena-export, ...)
  templates/ Jinja templates packaged and mounted by main.py
  static/    packaged CSS + HTMX + small JS helpers — no build step
tests/       pytest
docs/        architecture, operations, release evidence, roadmap, design notes
```

Templates, static assets, and `core/migrations/*.sql` are runtime package data.
Keeping them below `src/athena/` makes editable checkouts and installed wheels use
the same files from the same owner. The repository CI workflow is configured to
build the installable wheel through an extracted source distribution and run the
retained verification helpers, so both formats must preserve those runtime owners.

## Runtime health and schema integrity

`GET /healthz` is a cheap liveness signal. It does not open SQLite, so a process
can be alive even when its database is not ready.

`GET /readyz` is the database-backed readiness signal. It opens the configured
database and calls the same migration-status validator used by `athena-doctor`.
The migration runner requires a strictly numbered, contiguous packaged inventory
and a contiguous applied-ledger prefix; binds each applied migration to its
immutable, content-derived SHA-256 checksum; and rejects missing, duplicate,
unknown, future, unpackaged, out-of-order, or checksum-mismatched ledger state.
Startup applies pending migrations transactionally and then re-runs that
validator. Readiness checks the validated ledger but does not replace
`athena-doctor`'s full SQLite and attachment integrity checks.

There is no separate `api/` package: each module owns its REST surface
(`aegis/api.py`, `mentor/api.py`, `core/*_api.py`), so the code that serves
`/issues` lives next to the SQL that backs it.

The enforced dependency direction is `web -> (aegis | mentor) -> core`: web may
use either feature module and shared core, while Aegis and Mentor are independent
peers that may use core but never each other. Core never depends on a feature or
web module, and no layer imports `main.py`, the composition root. Run
`python scripts/check_import_contracts.py` to verify direct, indirect, and
supported literal dynamic imports. Dynamic checking accepts bare
`__import__` calls and direct
`importlib.import_module` calls after an unaliased, direct
top-level `import importlib`; literal external targets remain
outside the layer graph. Imports of `builtins` or frozen import machinery,
aliased or submodule imports of `importlib`, re-exports of loader names, and
direct loader/module escapes fail closed. Explicit acquisitions through
`.importlib`, `.builtins`, or machinery-module keys in literal mapping
subscripts or `.get(...)` also fail closed. Loader names reached from a module
name bound by an `import` statement through `getattr(...)`, attributes, or
`.__dict__` lookups also fail closed. Explicit `__builtins__` access and all
wildcard imports also fail because their bindings cannot be statically
inventoried. Arbitrary computed reflection, `eval`/
`exec`, and custom import hooks are outside this static policy.
The checker reserves the built-in `__builtins__`,
`__import__`, and `getattr` names. CI runs the same
check without importing application code.

## Roadmap (dogfood-first)

- **Phase 0 — Project setup** *(done)*: repo, skeleton, dev environment,
  first commit, GitHub remote.
- **Phase 1 — Core + Aegis** *(done)*: auth, users, agent tokens, DB +
  migrations, FTS search, activity log; issues/projects/statuses/boards +
  REST API + web UI.
- **Phase 2 — Mentor** *(done)*: spaces, page tree, versions (with restore),
  and the cross-links/backlinks that justify one workspace — tracked in Aegis
  along the way.
- **Phase 3 — Migration tooling** *(current)*: the generic machinery is built —
  selective export bundles, read-only dry-run validation, replay manifests,
  manifest-gated import, plus Jira/Confluence source mappers. Still open: the
  markdown-vault (wikilinks) → Mentor and hosted-tracker → Aegis mappers,
  then dry-run, verify, cut over.
- **Phase 4 — Fleet wiring & polish** *(largely done, landed alongside 2-3)*:
  MCP server, outbound webhooks, event and bounded UTC schedule automation rules,
  durable per-slot/per-target schedule receipts with synthetic activity lineage,
  cross-module search,
  backups/restore/doctor, run replay & lineage export, idempotent writes,
  per-token and anonymous rate limits, OIDC SSO, cooperative run check-ins,
  self-service delegation pickup, and agent admin dashboards. Remaining:
  (optional) self-host on `flow`.

## Security

- Config is environment-driven (`config.py`); secrets live in the environment,
  never in the repo (`.gitignore` excludes `.env` files). Nothing loads a
  `.env` automatically — inject it via your shell or process manager (e.g.
  systemd `EnvironmentFile=`).
- Browser sessions use HttpOnly, SameSite=Lax cookies and synchronizer CSRF
  tokens on every state-changing form (submitted as a form field or an
  `X-CSRF-Token` header). The login form, which has no session yet to bind a
  token to, rejects cross-site POSTs by Origin check instead (login-CSRF
  defense). Changing a password revokes the user's other sessions.
- Cookies carry the `Secure` flag only when `ATHENA_COOKIE_SECURE=1`; the app
  warns at startup when it's off so an HTTPS deploy doesn't ship insecure by
  accident. With it on, responses also send HSTS.
- Every response gets hardening headers (CSP `default-src 'self'`,
  `X-Frame-Options: DENY`, `nosniff`, ...), and request bodies are capped
  (`ATHENA_MAX_REQUEST_BODY_BYTES`).
- Authenticated REST mutations support durable, bounded `Idempotency-Key`
  single-flight claims. Exact retries coalesce across workers and completed
  receipts survive restarts; mismatched payloads conflict, revoked credentials
  cannot replay, and one-time-secret creation is excluded. Because domain writes
  and receipt finalization are not yet one transaction, abandoned/failed owners
  remain explicitly indeterminate and are never automatically taken over. A
  global authorization revision purges and permanently fences stored responses
  after access-affecting role, membership, visibility, authority, or placement
  changes; this is intentionally broader than per-target invalidation.
- Strong, content-derived issue `ETag` validators cover the exact public singleton
  representation. Core issue edits plus assignee/project/sprint placement accept
  optional `If-Match`; authorization and validation precede an atomic comparison
  and write, preventing two read-modify-write loops from committing the same tag.
- Users have coarse roles: `admin`, `member`, and read-only `viewer`. The first
  user is bootstrapped as admin, and the last admin cannot be demoted.
- Per-agent API tokens are scoped (`read`, `issue:write`, `docs:write`, `admin`).
  Command-backed durable mutations record *who* did them; the explicit remaining
  command-migration debt is tracked in [`COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md).
  Scopes narrow bearer tokens but never expand a user's role. Bearer traffic is
  rate-limited per token
  (`ATHENA_TOKEN_RATE_LIMIT_PER_MINUTE`); anonymous reads can be throttled per
  client IP (`ATHENA_ANON_RATE_LIMIT_PER_MINUTE`, off by default).
- Optional OIDC single sign-on activates only when all four `ATHENA_OIDC_*`
  connection settings are present; first-login auto-provisioning can be locked
  to an email-domain allow-list. Local email+password login is unaffected.
- The `X-Athena-Actor` fallback is disabled by default and should be enabled only
  on trusted local/tailnet deployments, usually just long enough for headless
  token bootstrap.
- When/if hosted: dedicated system user + systemd sandboxing, tailnet-only by
  default. Public exposure would be a deliberate, separate decision. See
  [`OPERATIONS.md`](OPERATIONS.md) for the deployment checklist.
