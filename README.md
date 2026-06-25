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
audited activity trail. It is still developed locally and is not hosted yet;
self-hosting on the `flow` node is a later decision.

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

- `/healthz` is a cheap liveness check.
- `/readyz` checks that SQLite is reachable and migrated.
- `ATHENA_MAX_REQUEST_BODY_BYTES` caps request bodies; it defaults to 1 MiB.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the full design and phased roadmap.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — deployment, health checks, backups, and restore.
