# Athena

Self-hosted **project management + knowledge base** — one platform, two modules:

- **Aegis** — work tracking: issues, boards, the "shield" over your work.
- **Mentor** — docs / knowledge base: the guide that holds what you know.

> Named for the goddess of wisdom *and* strategic craft. **Aegis** is her shield;
> **Mentor** is the guise she takes in the *Odyssey* to give counsel. Both are facets
> of Athena — so the platform and its two halves are one designed whole.

## Status

**Local alpha.** Athena has a working FastAPI app with Aegis issues/projects/boards,
Mentor spaces/pages/versioning, auth/sessions, CSRF, search, cross-links, and an
audited activity trail. Operators can export one project or space, map small
Jira/Confluence JSON exports into Athena bundles, dry-run against another Athena
database, write a replay manifest, and import transactionally. It is still
developed locally and is not hosted yet; self-hosting on the `flow` node is a
later decision.

## Stack

Python 3.12+ · FastAPI + Jinja2 + HTMX · SQLite (WAL). No JavaScript build chain.

## Why it exists

A self-owned replacement for Jira + Confluence: local control, no recurring SaaS
fees, and — uniquely — designed so **both humans and an AI fleet are first-class
API actors** (agents can file/triage issues and read/write docs through the same
audited, token-scoped API humans use through the web UI).

## Local Development

```bash
pip install -e ".[dev]"
ruff check .
pytest -q
uvicorn athena.main:app --reload
```

## Operations

Athena is currently intended for local or tailnet deployment. The app migrates
SQLite on startup and exposes:

- `/healthz` — cheap liveness check.
- `/readyz` — SQLite reachability and migration readiness check.

First-run bootstrap creates the first user as `admin`. Browser admins can manage
users at `/admin/users` and scoped API tokens at `/settings/tokens`. See the
operations runbook for deployment settings, role behavior, token scopes, and
headless bootstrap commands.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the full design and phased roadmap.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — deployment, bootstrap, roles, and token scopes.
- [`docs/RUNS.md`](docs/RUNS.md) — deterministic run replay, lineage, and forking contract.
