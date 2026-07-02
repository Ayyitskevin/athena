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
2. **API-first.** Every action is a REST endpoint. The web UI (Jinja + HTMX) is
   just a thin layer over that API — so the AI fleet (Claude, Grok, Codex,
   Hermes) can act through the *same* endpoints, with scoped, audited tokens.
   "Grok closed AEGIS-88" is first-class history, not a mystery.
3. **Local-first, no premature hosting.** Built and version-controlled as a
   normal software project. Self-hosting on the `flow` node is a *later*
   decision, not a starting assumption.
4. **Greenfield until cutover.** While we build, Athena reads from and writes to
   nothing else. The existing systems (ORACLE for docs, Notion for tasks) stay
   the source of truth. At the end we run a one-time **migration**, not an
   ongoing sync — that avoids the "two systems, which one is right?" trap.

## Stack

- **Python 3.12+**, **FastAPI** (web framework), **Jinja2** (HTML templating),
  **HTMX** (interactivity without a JS build step).
- **SQLite** in WAL mode — one file, trivially backed up, plenty for one
  operator. Full-text search via SQLite's built-in **FTS5**.
- Filesystem store for attachments. No external services required to run.

## Layout (target)

```
src/athena/
  core/      auth, users, agent tokens, db, search (FTS5), attachments,
             activity log, the issue<->doc cross-link resolver
  aegis/     issues, projects, statuses, boards, sprints, labels, comments
  mentor/    spaces, pages (tree), versions
  api/       the REST surface
  web/       Jinja templates + HTMX (built on top of api/)
tests/       pytest
docs/        this file and design notes
```

## Roadmap (dogfood-first)

- **Phase 0 — Project setup** *(current)*: repo, skeleton, dev environment,
  first commit, GitHub remote.
- **Phase 1 — Core + Aegis**: auth, users, agent tokens, DB + migrations, FTS
  search, activity log → then issues/projects/statuses/boards + REST API + web
  UI. *Done when Kevin can run real work in it.*
- **Phase 2 — Mentor, tracked in Aegis**: spaces, page tree, versions, the
  cross-links. Every task for this phase is an Aegis issue — eat our own cooking.
- **Phase 3 — Migration tooling**: ORACLE (markdown + wikilinks) → Mentor pages;
  Notion Tasks → Aegis issues. Dry-run, verify, then cut over.
- **Phase 4 — Fleet wiring & polish**: Hermes/Odysseus push to Aegis; search
  across both modules; backups; (optional) self-host on `flow`.

## Security

- Secrets in a `.env` file, never committed (see `.gitignore`).
- Browser sessions use HttpOnly cookies and CSRF tokens for state-changing forms.
- Users have coarse roles: `admin`, `member`, and read-only `viewer`. The first
  user is bootstrapped as admin, and the last admin cannot be demoted.
- Per-agent API tokens are scoped (`read`, `issue:write`, `docs:write`, `admin`);
  every write records *who* did it. Scopes narrow bearer tokens but never expand
  a user's role.
- The `X-Athena-Actor` fallback is disabled by default and should be enabled only
  on trusted local/tailnet deployments, usually just long enough for headless
  token bootstrap.
- When/if hosted: dedicated system user + systemd sandboxing, tailnet-only by
  default. Public exposure would be a deliberate, separate decision. See
  [`OPERATIONS.md`](OPERATIONS.md) for the deployment checklist.
