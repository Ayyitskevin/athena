# AGENTS.md — contributing to Athena (humans and AI agents)

This is the contract for working in **this repo**. It is re-read every session
and travels with the code, so it — not a chat prompt — is the source of truth
for *how* we build here. Read it before you write.

For project-wide rules of conduct, defer to your machine-level handbook
(`~/.claude/CLAUDE.md` for Claude Code, `~/.grok/GROK.md` for Grok,
`~/.codex/AGENTS.md` for Codex) — including its canonical **Permission
Boundaries** block, which this file narrows for Athena (the PR-gate below is
that narrowing). This file adds the rules **specific to Athena** and wins on
Athena-specific conflicts.

---

## Mission (what we are building)

Athena is a **self-hosted operator workspace** — not a Jira/Confluence enterprise
clone and not a Notion/Obsidian UX clone.

**Product:** markdown docs + issue tracking + cross-links in one SQLite-backed
app, runnable on the operator's machine or tailnet.

**Audience:** solo operators and tiny teams (2–5) who want data ownership and run
AI agents alongside them.

**Differentiator:** agents are first-class actors — scoped bearer tokens, MCP
server, webhooks, idempotent writes, append-only activity log, run replay/lineage,
delegation/contributors. The audit log is load-bearing, not decorative.

**Modules:**

| Module | Path | Role |
|--------|------|------|
| **Mentor** | `src/athena/mentor/` | Knowledge — spaces, page tree, versions, wikilinks |
| **Aegis** | `src/athena/aegis/` | Work — issues, boards, sprints, labels, automation |
| **core** | `src/athena/core/` | Shared auth, db, search, links, activity, portability |

**In scope:** API-first design, self-host simplicity (single process, no JS build),
honest import/export, free OIDC, agent safety (scopes, rate limits, idempotency).

**Out of scope:** multi-tenant SaaS, JQL/custom fields, workflow engines, block
editors, Obsidian-style vault files, enterprise permission schemes, feature races
with Notion AI or Atlassian Rovo.

When in doubt, optimize for **operator + agent fleet on one machine**, not
enterprise parity or consumer polish.

Full design rationale: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## The cardinal rule: the web layer is a thin client over the API

Athena is **API-first** (ARCHITECTURE principle 2). Every piece of data has
exactly one home: the database, reached through the REST API in `aegis/`,
`mentor/`, and `core/`.

Therefore:

- **The web layer (`web/`) MUST NOT own data.** No in-memory lists, no sample
  dicts, no module-global stores, no parallel "stub" copies of anything the API
  already serves. Pages get their data by calling the real API (or the
  data-access functions behind it). A page with no data yet renders **empty** —
  it does not invent rows to look populated.
- If the endpoint you need doesn't exist, that is a **blocker to flag**, not a
  reason to fake it. Stop and say so (see "When scope grows").
- One concept, one owner. Two code paths that both "create an issue" is a bug,
  even if both pass their tests.

> Why this rule is first: the most common failure here is a UI that *looks* like
> it works against fake data, then silently loses everything on restart. That is
> the split-brain trap the whole architecture exists to avoid.

---

## Module ownership (stay in your lane)

| Area | Path | What lives here |
|------|------|-----------------|
| **core** | `src/athena/core/` | db, migrations, auth, users, agent tokens, search, cross-link resolver |
| **aegis** | `src/athena/aegis/` | issues/projects/statuses/boards — data access + REST API |
| **mentor** | `src/athena/mentor/` | spaces, pages, versions — knowledge module |
| **web** | `src/athena/web/` | Jinja templates + HTMX — **thin client over the API only** |

Touch your assigned area. Don't refactor a neighbor's module to make your change
fit — flag the friction instead. If two agents must change the same file (e.g.
`main.py` wiring), keep additions side-by-side and resolve in the PR.

---

## Branch, PR, and merge

- **Branch per agent:** `claude/<topic>`, `grok/<topic>`, `codex/<topic>`,
  `kevin/<topic>`. One logical change per branch, kebab-case topic.
- **Work in your own checkout/worktree.** Never edit another agent's working dir.
- **`main` is the truth.** Open a PR so the change has a record and the review
  tail can look at it. In Athena, **agents may merge their own PR** once it's
  green — this is a dev project, not a live service, so we optimize for flow.
  (This deliberately diverges from the fleet-wide "Kevin merges" rule, which
  still holds for live services like Mise.) Still **never push directly to
  `main`** — the PR is the gate, even when an agent merges it.
- Rebase on `main` before opening the PR (linear history). Never force-push
  `main`; force-with-lease on your *own* feature branch after a rebase is fine.

---

## Definition of done (all must hold before you call it done)

1. `ruff check .` and `pytest -q` are **green** — no skipped or mocked-away
   tests passed off as passing.
2. You **ran it**: the app boots and the feature works against the real DB
   (`uvicorn athena.main:app`, hit the route). "Should work" is not "works."
3. **No stray data stores** — grep your diff for in-memory lists/dicts standing
   in for the database. There should be none (see the cardinal rule).
4. Tests encode *why* the behavior matters, not just that a route returns 200.
5. You stayed in scope. Anything extra you were tempted to add → note it in the
   PR as a follow-up, don't smuggle it in.

---

## When scope grows or you hit a blocker

If the task turns out bigger than stated, or you need something that doesn't
exist yet: **stop and restate the new scope, then wait.** Do not quietly absorb
the extra work, and do not fake a dependency to keep moving. A flagged blocker is
cheap; a hidden assumption shipped to `main` is expensive.

---

## Quick orientation

```
src/athena/
  core/      db + migrations + (later) auth/users/tokens/search/cross-link
  aegis/     issues API (issues.py = SQL, api.py = HTTP)
  mentor/    spaces, pages, versions — knowledge module
  web/       templates + HTMX — thin client over the API
  config.py  env-driven settings (ATHENA_DB, ...)
  main.py    app factory: create_app(), /healthz, migrate-on-startup, wiring
tests/       pytest
docs/        ARCHITECTURE.md — the design of record
```

Run the gate: `ruff check .` and `pytest -q`.
Run the app: `uvicorn athena.main:app --reload`.
