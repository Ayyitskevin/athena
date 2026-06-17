# Athena

Self-hosted **project management + knowledge base** — one platform, two modules:

- **Aegis** — work tracking: issues, boards, the "shield" over your work.
- **Mentor** — docs / knowledge base: the guide that holds what you know.

> Named for the goddess of wisdom *and* strategic craft. **Aegis** is her shield;
> **Mentor** is the guise she takes in the *Odyssey* to give counsel. Both are facets
> of Athena — so the platform and its two halves are one designed whole.

## Status

🚧 **Phase 0 — project setup.** Developed locally, version-controlled on GitHub.
Not hosted yet; self-hosting on the `flow` node is a later decision.

## Stack

Python 3.12+ · FastAPI + Jinja2 + HTMX · SQLite (WAL). No JavaScript build chain.

## Why it exists

A self-owned replacement for Jira + Confluence: local control, no recurring SaaS
fees, and — uniquely — designed so **both humans and an AI fleet are first-class
API actors** (agents can file/triage issues and read/write docs through the same
audited, token-scoped API humans use through the web UI).

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the full design and phased roadmap.
