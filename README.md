# Athena

**Self-hosted operator workspace** — markdown docs, issue tracking, and cross-links
in one place. Built for **solo operators** who work alongside an **AI fleet**.

> Notion's shape. Your machine. Built for agents.

Athena replaces the scattered stack many solo operators run today — Notion or
Obsidian for notes, a separate tool for tasks, and brittle AI integrations on
top. One SQLite file, one FastAPI app, full data ownership, no SaaS rent.

## Mission

Athena is an **open-source, self-hosted workspace** for people who:

- Want **docs + tasks together** (not Jira *and* Confluence *and* Notion)
- Need **data on their own machine** (tailnet or local — not a vendor cloud)
- Run **AI agents as real teammates** — scoped tokens, MCP, webhooks, audited
  writes, deterministic run replay, and delegation

We are **not** chasing Atlassian enterprise parity, Notion's block editor, or
Obsidian's local vault + plugin ecosystem. We **are** building the command center
a solo operator and their agents can trust.

## Two modules, one workspace

- **Mentor** — knowledge: spaces, page tree, markdown bodies, version history,
  wikilinks (`[[page:7]]`), backlinks, full-text search
- **Aegis** — work: issues, boards, sprints, labels, filters, dependencies,
  automation — linkable from docs via `[[issue:42]]` or `[[ATH-12]]`

Named for the goddess of wisdom *and* strategic craft. **Mentor** is the guise
she takes in the *Odyssey* to give counsel; **Aegis** is her shield. Both are
facets of one designed whole.

## Who this is for

| Good fit | Not a fit (yet) |
|----------|-----------------|
| Solo operators, indie hackers, small tailnet teams | 20+ person eng orgs expecting JQL and custom fields |
| People leaving Notion/Obsidian + scattered task tools | Teams wanting Notion-grade block editors |
| Operators running Claude, Codex, Grok, or custom agents via API/MCP | Enterprises needing SCIM, SAML, and scheme-based permissions |
| Self-hosters who want OIDC without a paywall | Anyone who needs multi-tenant SaaS today |

## Status

**Local alpha.** Working FastAPI app: Mentor spaces/pages/versioning, Aegis
issues/projects/boards/sprints, auth/sessions/OIDC, CSRF, FTS5 search,
cross-links, webhooks, automation, portability import/export, and an append-only
activity trail with run replay. Developed locally; production self-host on the
`flow` node is a later decision.

## Stack

Python 3.12+ · FastAPI + Jinja2 + HTMX · SQLite (WAL) · FTS5. No JavaScript
build chain.

## Why it exists

Cloud workspaces (Notion, etc.) charge recurring fees, gate AI behind paid
tiers, and never give you a replayable audit log of what your agents did.
Obsidian is excellent for local notes but has no unified task layer or
first-class agent API.

Athena is the alternative: **one self-owned workspace where humans and agents
share the same audited, token-scoped API** — file issues, write docs, cross-link
work and knowledge, and replay any agent run from the log.

## Local development

```bash
pip install -e ".[dev,mcp]"
ruff check .
pytest -q
python scripts/smoke_app.py
uvicorn athena.main:app --reload
```

From a fresh virtual environment, reproduce the dependency versions used by
Linux/Python 3.12 CI by installing the same extras through
[`constraints/ci-py312.txt`](constraints/ci-py312.txt):

```bash
pip install -c constraints/ci-py312.txt -e ".[dev,mcp]"
```

The smoke helper uses an inherited POSIX socket, matching Ubuntu CI and Athena's
supported deployment shape, and verifies the fresh database, rendered home page,
and packaged stylesheet. CI builds a source distribution, builds the wheel from
that extracted archive, and compares the wheel with both the checkout and archive
runtime manifests before installing and booting it outside the checkout. The
wheel's templates, static assets, and migrations must match exactly. The full
contributor install above includes MCP test coverage; for a runtime-only MCP
install, use `pip install -e ".[mcp]"` then `athena-mcp`.

## Operations

Intended for local or tailnet deployment. The app migrates SQLite on startup
and exposes:

- `/healthz` — liveness
- `/readyz` — SQLite reachability and migration readiness

First-run bootstrap creates the first user as `admin`. Browser admins manage
users at `/admin/users` and scoped API tokens at `/settings/tokens`.

## Documentation

- [`AGENTS.md`](AGENTS.md) — mission and contributor contract (read this first if you are an AI agent)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design of record and phased roadmap
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — deployment, bootstrap, roles, token scopes
- [`docs/RUNS.md`](docs/RUNS.md) — deterministic run replay, lineage, and forking
- [`docs/WORK_CONTEXT.md`](docs/WORK_CONTEXT.md) — bounded actor-visible issue context for agents
