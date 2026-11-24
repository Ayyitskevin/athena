# AGENTS.md — contributing to Athena (humans and AI agents)

This is the contract for working in **this repo**. It is re-read every session
and travels with the code, so it — not a chat prompt — is the source of truth
for *how* we build here. Read it before you write.

For project-wide rules of conduct, defer to your machine-level handbook (for
Claude Code that is `~/.claude/CLAUDE.md`; other agents use their own
equivalent) — including its canonical **Permission Boundaries** block, which
this file narrows for Athena (the PR-gate below is that narrowing). This file
adds the rules **specific to Athena** and wins on Athena-specific conflicts.

**When no machine-level handbook is present** (a cloud container, a fresh
machine, a runtime that has none), the rules below stand in for it. They are a
floor drawn from this repository's own rules and accepted decisions, not a copy
of the handbook; where a handbook is present, it governs.

- **Stay in this repository.** Read and write only this checkout and its GitHub
  repository. Never touch another repository, a live service or another
  agent's working directory.
- **Branch and PR.** Work on your lane's branch, never push `main`, and land
  every change through a PR (see "Branch, PR, and merge").
- **The owner's acts stay the owner's.** Do not create or push a tag, publish a
  release or a container image, set a prune verdict, answer an item in
  [`docs/DECISIONS_PENDING.md`](docs/DECISIONS_PENDING.md), close another
  lane's PR or delete branches.
- **Ask before anything destructive or hard to undo:** rewriting history on a
  shared branch, deleting files or data you did not create, changing repository
  settings.
- **No secrets in the record.** Never commit a credential or put one in a log,
  a PR, an issue or a document.
- **Do not weaken the gate.** Never lower a coverage floor, extend
  `ALLOWED_PRAGMAS`, add a checker exemption, or skip or delete a test to get
  green. Fix the code or add the test.
- **Migrations are forward-only**, and hidden things read as missing (404),
  never as forbidden (403).

Prepare a fresh checkout with `scripts/bootstrap.sh`: full history, and the
`.venv` built as [CONTRIBUTING.md](CONTRIBUTING.md) builds it. A Claude Code
cloud session runs it at start through the committed `.claude/` hook, and
`.claude/settings.json` allow-lists the gate and the bootstrap, nothing else.

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

**North star:** [`docs/VISION.md`](docs/VISION.md) — *mission control for a one-person
agent fleet*. It sharpens the mission into the operator's loop (direct → delegate →
observe → intervene → trust) and five steering rules every change is measured against.
Read it before proposing a feature; if a change doesn't help a solo operator conduct
their agents, it doesn't belong here.

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
Read the [glossary and decision index](docs/GLOSSARY.md) before changing domain
meanings. New consequential decisions need a dated record with a cited accepting
source and supersession links; recommendations remain proposals until accepted.

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
- **One command owns each write (migration rule).** REST and browser handlers
  should be transport adapters: they parse HTTP input and translate the same
  framework-neutral application command's result/error into JSON or HTML. The
  command owns domain authorization, normalization, validation, persistence,
  projections, and the activity event in one transaction. Issue create/core-edit
  is the first migrated slice; legacy mutation+activity pairs remain migration
  debt, not a pattern to copy. Any new write—or existing split write you change—
  must move behind a shared command rather than adding another transport copy.
- **One command-error dialect.** A command refuses with a transport-neutral
  *kind* — a closed string vocabulary per command module (`"not_found"`,
  `"invalid"`, `"conflict"`, ...) plus a human `detail` — never an HTTP status
  code. The adapter owns the kind→status mapping (`_STATUS_BY_KIND` tables in
  the route module), so the same refusal can be a 422 in REST and a 400 in a
  form without the command knowing a transport exists. Every command module
  carries the target shape now — the last `status_code`-carrying holdouts
  (`automation_commands`, `status_commands`, `agent_commands`,
  `attachment_commands`, and `project_commands`' access errors) were migrated
  2026-08-24, so `grep -l status_code src/athena/*/*commands*.py` returns
  nothing, and that grep — not this sentence — stays the authority. Do not
  reintroduce the old shape into anything new. `ApprovalDecisionError` lives
  in `core/approval_commands.py` with a `kind` and a `detail`; the route
  adapter maps the kind to a status.
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
| **workflows** | `src/athena/workflows/` | commands that span BOTH modules (read one, write the other) — the only place that may import `aegis` and `mentor` together |
| **mcp** | `src/athena/mcp/` | the optional `athena-mcp` server: tools declared in `tools_*.py`, calling the REST API through `client.py` — a transport, like `web/` |
| **templates**, **static** | `src/athena/templates/`, `src/athena/static/` | the Jinja templates and vendored assets `web/` serves (read `docs/DESIGN_SYSTEM.md` first) |
| **field_guide** | `src/athena/field_guide/` | the agent Field Guide's pages, as markdown; `guide.py` seeds them as a space |

Touch your assigned area. Don't refactor a neighbor's module to make your change
fit — flag the friction instead. If two agents must change the same file (e.g.
`main.py` wiring), keep additions side-by-side and resolve in the PR.

---

## Change recipes (which files a change touches)

Read the row before you write. Paths are from the checkout root; run scripts with
`.venv/bin/python`. [CONTRIBUTING.md](CONTRIBUTING.md) has the detail behind each
check, generator and the changed profile's selection.

| Change | Edit | Then run | Tests (then `scripts/gate.sh --profile changed`) |
|---|---|---|---|
| MCP tool | `src/athena/mcp/tools_<area>.py` (`@tool`/`@mutation_tool` declares scope, profiles, hints, budget) and its method in `src/athena/mcp/client.py` | `scripts/generate_agent_api.py` | `tests/test_mcp_tool_declarations.py`, `tests/test_mcp_tool_scopes.py`, `tests/test_mcp_response_budget.py`, `tests/test_agent_api_doc.py` |
| Agent-facing REST route | `src/athena/<pkg>/<name>_api.py` (refuse with `Refusal(code=...)`); a body derives from `RequestBody` in `src/athena/core/request_models.py`; a new router is included in `src/athena/main.py` | `scripts/generate_agent_api.py` if a tool calls it | `tests/test_agent_openapi.py`, `tests/test_request_bodies_closed.py`, `tests/test_refusal_codes.py`, `tests/test_write_ownership.py` |
| Browser route | `src/athena/web/<name>.py` and `src/athena/templates/` (read `docs/DESIGN_SYSTEM.md` first); a new router is included in `src/athena/main.py` with `browser_dependencies` | — | `tests/test_agent_openapi.py`, `tests/test_template_contracts.py`, `tests/test_csp_no_unsafe_inline.py` |
| Refusal code | `src/athena/core/refusal_codes.py` | `scripts/generate_agent_api.py` | `tests/test_refusal_codes.py` (runs `scripts/check_refusal_codes.py`), `tests/test_agent_api_doc.py` |
| Write command | `src/athena/<pkg>/<name>_commands.py` (takes `actor`, refuses with a `kind`, passes `commit=` to writers); the route's `*STATUS_BY_KIND` and `*CODE_BY_KIND` | `scripts/generate_command_debtors.py` | `tests/test_transaction_commits.py`, `tests/test_write_ownership.py`, `tests/test_command_error_dialect.py`, `tests/test_command_debtors_doc.py` |
| Migration | a new `src/athena/core/migrations/NNNN_<name>.sql`, next number; never edit an applied one | — | `tests/test_migration_integrity.py`, `tests/test_migrations_atomic.py` (the changed profile runs the full suite) |
| Setting | a `Setting` in `src/athena/config.py` | `scripts/generate_settings_doc.py` | `tests/test_settings_registry.py` |
| Activity reader | the reader, and a `[[guarded]]` or `[[exempt]]` entry in `src/athena/<pkg>/imported_at_guards.toml` | — | `tests/test_imported_at_guards.py` |
| Free text in an agent packet | the response model carries `authored_by` (`src/athena/core/authorship.py`) or the field joins `NOT_PEER_WRITTEN` in its test | — | `tests/test_peer_text_authorship.py` |
| Entry document | `docs/AGENT_GUIDE.md`, `docs/AGENT_BOOT.md`, `docs/RUNTIME_RECIPE.md`, `docs/QUICKSTART.md`, `docs/CLOSEOUT_RECIPE.md`; its examples run | — | `tests/test_doc_examples.py`, `tests/test_docs_index.py`, `tests/test_agent_guide.py` |
| Changelog entry | `changelog.d/<slug>.<category>.md`, never `CHANGELOG.md` | `scripts/changelog_fragments.py check` | `tests/test_changelog_fragments.py` |
| Test | `tests/test_<topic>.py`, starting from `tests/support.py`; no `time.sleep` | — | `tests/test_suite_conventions.py` |

---

## Branch, PR, and merge

- **Branch per agent:** `claude/<topic>`, `grok/<topic>`, `codex/<topic>`,
  `kevin/<topic>`. One logical change per branch, kebab-case topic.
- **Work in your own checkout/worktree.** Never edit another agent's working dir.
- **`main` is the truth.** Open a PR so the change has a record and the review
  tail can look at it. In Athena, **agents may merge their own PR** once it's
  green — this is a dev project, not a live service, so we optimize for flow.
  (This deliberately diverges from the fleet-wide "the operator merges" rule,
  which still holds for live production services.) Still **never push directly
  to `main`** — the PR is the gate, even when an agent merges it.
- Rebase on `main` before opening the PR (linear history). Never force-push
  `main`; force-with-lease on your *own* feature branch after a rebase is fine.

---

## Definition of done (all must hold before you call it done)

1. Run `scripts/gate.sh` (default `product` profile): it executes the local product
   inventory in `scripts/gate.py`, including dependency validation, lint, formatting,
   type checks, architectural checks (import contracts, write ownership, transaction
   commits, `imported_at` guards, template styles and routes) and full-suite
   coverage. Explicit `--profile quick`
   is partial and cannot replace product evidence. Nor can `--profile changed`, the
   inner loop: while you work, run it for the static stages, the type check and the
   tests selected from your diff (see CONTRIBUTING.md); the product profile stays
   the condition of done. The product profile does not run CI's container
   or release-candidate audit jobs; obtain those separately. If the gate changes, change
   AGENTS.md, CONTRIBUTING.md, the script and `.github/pull_request_template.md`
   together. The template asks for the gate receipt (profile, commit and closing
   lines, or the file `scripts/gate.sh --json <path>` writes) as the PR's evidence.
   At the default four test workers the product gate needs about
   1.5 GiB (1.40 GiB measured on 2026-11-22) of memory. On a
   smaller machine set `ATHENA_COVERAGE_WORKERS` to fewer; the receipt says when a
   worker was killed.

2. You **ran it**: the installed `athena-serve` entrypoint passes preflight,
   boots against a real DB, and the feature works over real HTTP. The retained
   `scripts/smoke_app.py` lifecycle is the minimum release-path proof; hit the
   changed route too. "Should work" is not "works."
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

Agents working through Athena's API should read [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md) first.

```
src/athena/
  core/      db + migrations + auth/users/tokens/search/cross-link/deployment
  aegis/     work module (issues.py = SQL, issue_commands.py = audited writes,
             api.py = HTTP)
  mentor/    spaces, pages, versions — knowledge module
  workflows/ commands that span aegis and mentor
  web/       templates + HTMX — thin client over the API
  mcp/       athena-mcp: tools_*.py over client.py (REST)
  templates/ Jinja templates the web layer renders
  static/    vendored assets the pages load
  field_guide/  the agent Field Guide's markdown pages
  config.py  env-driven settings: the SETTINGS registry (ATHENA_DB, ...)
  main.py    app factory: create_app(), deployment boundary, health, wiring
  middleware.py  request middleware the app factory installs
  agent_openapi.py  the agent-facing OpenAPI document (/openapi-agent.json)
  guide.py   seeds the Field Guide as a space
  demo.py    athena-demo: a disposable, seeded review workspace
  ops.py     athena-serve and the retained operator CLIs
  exposure.py  what an instance answers without a credential (athena-doctor, first start)
tests/       pytest; support.py is where a new test starts
docs/        INDEX.md — one-line map of every doc · ARCHITECTURE.md — the design of record
```

Run `scripts/gate.sh` for the complete local product profile (also selectable with
`--profile product`), `scripts/gate.sh --profile changed` as the inner loop (partial:
the tests your diff reaches, no coverage floors), or `scripts/gate.sh --profile quick`
for the static stages alone.
The receipt names the commit, worktree state, Python/import identity, run and omitted
stages, and CI-only jobs; the changed profile's receipt also lists what it selected
and why. See CONTRIBUTING.md for the CI and release evidence boundaries.
The wrapper uses this checkout's `.venv/bin/python` without activation and refuses
with a CONTRIBUTING.md setup pointer if that executable is unavailable. Inventory
tests compare complete CI command lines after dropping blank and comment-only
lines, and reject unnamed steps with commands. Action version bumps do not change that
inventory.
Run the app: configure the absolute storage paths and bootstrap/start through
`athena-serve` as documented in `docs/OPERATIONS.md`. Raw Uvicorn startup is an
unsupported development escape hatch and intentionally has no authority unless
one is configured explicitly.
