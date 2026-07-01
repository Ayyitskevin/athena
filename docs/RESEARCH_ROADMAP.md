# Athena — Research Report & Implementation Roadmap

> **Status:** Historical research deliverable plus current-state reconciliation.
> **Date:** 2026-06-26 · **Branch:** `claude/athena-research-roadmap`
> **Reconciled:** 2026-06-30 against `main` through `8bd9a1c`.
> **Scope:** What it would take to make Athena a serious, self-hosted, agent-native
> alternative to Jira + Confluence — learning from them without cloning them.

**How to read this doc.** Claims are tagged **[Fact]** (sourced, verifiable),
**[Interpretation]** (my reading of the facts), or **[Recommendation]** (what
Athena should do). External claims are cited inline. The one viral insider video
is treated as anecdotal and corroborated separately (§5). Be warned: §2 is blunt
about what Athena is missing, and §7/§11 are equally blunt about what it should
*not* build.

---

## 1. Executive Summary

Athena is a small, clean, genuinely API-first core: two modules (Aegis = issues,
Mentor = docs) over one SQLite database, sharing auth, a unified FTS5 search
index, a cross-link resolver (`[[issue:N]]` / `[[page:N]]`), and an append-only
audit/event log. Every write is attributable to a human or an agent through
scoped bearer tokens. That shared-database, one-audit-trail,
agent-as-first-class-actor foundation remains Athena's differentiator.
**[Interpretation]**

This document began as a June 26 research snapshot. Many structural gaps it named
have since landed: read visibility/membership, Markdown rendering with
sanitization, attachments, webhooks/events, notifications, per-project statuses,
issue hierarchy, sprints, saved filters, OIDC, idempotency, official MCP access,
agent delegation, run lineage/forking, and deploy preflight checks. Treat the
original "absent" lists below as historical context unless the 2026-06-30
addendum or current code says otherwise. **[Fact, from code]**

The current remaining gaps are narrower:

- **Portability is still coarse.** Athena has whole-DB backup/restore and deploy
  preflight, but no per-project/per-space JSON export or dry-run import. **[Fact]**
- **Run replay is not yet a packaged artifact.** `/events`, lineage, and fork
  coordinates exist, but there is no single replay manifest/bundle endpoint or CLI
  that freezes a run for handoff, audit, or rehydration. **[Fact]**
- **Agent/team administration is still basic.** Agent users can be delegated issues,
  but there is no dedicated admin surface for agent scope, allowed projects/spaces,
  or delegation policy. **[Fact]**
- **API ergonomics are partial.** Idempotency exists for POST retries; bulk
  operations, per-token rate limiting, and ETag/`If-Match` concurrency are still
  open. **[Fact]**
- **Packaging and retention need hardening.** Athena's single-file posture is a
  differentiator, but the repo does not yet ship a one-command production package
  or backup retention/off-host helper. **[Fact + Recommendation]**

The market context sharpens the opportunity. Atlassian killed Server (end of
support **Feb 15, 2024**), pushing small self-hosters toward expensive Cloud
seats or a 500-user-minimum Data Center license; it then cut **~10% of staff
(~1,600 roles) in March 2026** to "self-fund AI," and is bolting AI agents (Rovo)
*onto* a product that developers persistently call slow and over-engineered.
Meanwhile the open-source alternatives are fragmenting (Plane for issues,
Outline/BookStack/XWiki for docs, OpenProject for PM) and almost none combine
issues + docs + a real audit trail + agent-native APIs in one self-hosted unit. **[Fact + Interpretation]**

**The thesis (§7):** Athena should not chase Jira's depth or Confluence's macro
sprawl. It should be the *smallest system a solo operator and an AI fleet can run
forever*: one file, one API, one audit log, issues and docs that cross-link and
search together, and an agent surface (webhooks + MCP + idempotent bulk API) that
is **native, not bolted on**. The next six months of work should buy *substance*
(portability, replay bundles, agent/team administration, API safety, packaging)
before any more UI polish.

**Build next (§12):** (1) per-project/per-space export, (2) dry-run import, (3) run
replay manifest/bundle, (4) agent administration/policy, (5) API safety follow-ups
(rate limiting, bulk endpoints, ETags) and self-host packaging.

---

## 2. Current Athena Baseline

This section was derived entirely from reading the repository at the original
research branch (no external sources). It is preserved as a historical baseline,
not the current source of truth. See §2.7 for the 2026-06-30 implementation delta.

### 2.1 Stack & shape **[Historical Fact]**
- Python 3.12+, FastAPI, Jinja2, HTMX, SQLite (WAL), FTS5. No JS build chain
  (`static/htmx.min.js`, hand-written `confirm.js`). Dependencies are deliberately
  tiny: `fastapi, jinja2, python-multipart, uvicorn[standard]` (+ `pytest, httpx,
  ruff` for dev). `pyproject.toml`.
- App-factory pattern (`create_app()` in `main.py`), migrate-on-startup, `/healthz`
  (liveness) and `/readyz` (DB + migration readiness). 20 forward-only SQL migrations.
- Hardening already present: strict CSP and security headers, `RequestBodyLimitMiddleware`
  (enforces a body cap even for chunked requests), HttpOnly session cookies,
  synchronizer-token CSRF on browser writes, optional `Strict-Transport-Security`.
- ~50 test files under `tests/`; CI is `.github/workflows/ci.yml` (ruff + pytest).

### 2.2 Data model (20 migrations) **[Historical Fact]**
`users` (email, name, password_hash, `role`), `issues` (title, body, status,
priority, created_by, assignee_id, project_id, project_seq), `api_tokens`
(token_hash, `scopes`), `sessions` (csrf_token), `comments` (issue-only),
`labels` + `issue_labels` (m2m), `projects` (key, `issue_counter`), `spaces` (key),
`pages` (space_id, parent_id tree, updated_by/at), `page_versions` (full history),
`links` (cross-link index, re-derived from body on write), `search_index` (FTS5
over issues+pages), `issue_links` (typed `blocks`/`relates` dependencies),
`activity` (append-only audit).

### 2.3 Aegis (issues) **[Historical Fact]**
- **Lifecycle is fixed and global:** `STATUSES = ("open","in_progress","done")`,
  `PRIORITIES = ("low","medium","high","urgent")` — hard-coded constants in
  `aegis/issues.py`, validated at the API/web boundary. Boards (`/aegis/boards`)
  are just `list_issues` grouped into one column per status.
- Project keys (Jira-style `ATH-12`) via a monotonic per-project counter; numbers
  are never reused, even across moves/deletes. Issues addressable by id or key.
- Labels (find-or-create, color-validated), comments (with edit/delete by author),
  optional assignee, optional project.
- **Typed dependencies are advisory:** `blocks` / `blocked_by` / `relates`. Closing
  an issue with open blockers shows a warning but a second submit ("Mark done
  anyway") proceeds — by design.
- **Write authorization = creator OR current assignee** (`issues.can_modify`).
  Project/space *deletes* are creator-only. There is no project-level role or
  membership; "any authenticated non-viewer" can create issues, labels, projects.

### 2.4 Mentor (docs) **[Historical Fact]**
- Spaces (short key like `ENG`), a page tree (`parent_id` must be in the same
  space; cycle-checked on move), full version history with restore, backlinks.
- **Edits follow a shared-wiki model:** any authenticated non-viewer may create/
  edit/move pages; only the space creator may delete a space, and a non-empty
  space/page refuses deletion (no silent cascade).
- **At this snapshot:** page comments, page labels, attachments, and Markdown body
  rendering had not landed yet.

### 2.5 Core (the shared spine) **[Historical Fact]**
- **Identity:** bearer token (SHA-256 hashed, scoped) is primary; `X-Athena-Actor`
  header is an off-by-default local-trust fallback used only for headless bootstrap.
- **Roles (coarse, global):** `admin` / `member` / `viewer`. Last admin can't be
  demoted. **Token scopes** (`read`, `issue:write`, `docs:write`, `admin`) narrow
  a bearer token *below* the user's role but never expand it.
- **Cross-links:** `core/links.py` parses `[[issue:N]]`, `[[page:N]]`, `[[ATH-12]]`
  from body text on every write, indexes them, resolves existence at read time,
  and renders broken refs visibly. Backlinks are a first-class query.
- **Search:** one FTS5 index spans issues + pages; bm25 with title weighted above
  body; safe prefix-AND query builder; snippet highlighting. (The issue *list*
  filter, separately, uses SQL `LIKE`.)
- **Audit:** `activity` records `(actor, verb, target_kind, target_id, detail)` for
  creates, status/priority/assignee/project changes, comments, labels, links, and
  all Mentor space/page events. Browser feed at `/aegis/activity` with actor/verb/
  kind/target/search filters, cursor paging, and a CSV export (newest 1000 rows).
- **Ops:** `athena-backup` / `athena-restore` CLIs (SQLite online-backup API; refuse
  to clobber without `--overwrite`/`--force`; clean up `-wal`/`-shm` sidecars).

### 2.6 What was *absent* on 2026-06-26 **[Historical Fact]**
The original code-read found no read authorization, no per-space/project visibility
or membership, no Markdown/rich text, no attachments, no notifications/watching/
mentions, no webhooks or outbound event stream, no MCP server, no custom statuses/
workflows, no issue hierarchy, no sprints/cycles, no saved filters, no page
comments/labels, no SSO/OIDC/SCIM, no granular import/export, no bulk API, no
per-token rate limiting, and no idempotency keys. Many of those gaps are now closed.

### 2.7 2026-06-30 implementation delta **[Fact, from current main]**
- Database migrations have advanced through `0040_activity_forked_from_event.sql`.
- Bodies render through Markdown-it with raw HTML disabled and `nh3` sanitization,
  while preserving cross-links and mentions.
- Attachments exist for issues and pages with randomized stored names, size caps,
  authenticated downloads, deletes, and activity records.
- Project/space visibility and membership gate reads across detail pages, lists,
  search, activity, notifications, cross-links, and attachments.
- `/events`, webhooks, notifications/watching/mentions, automation rules, and an
  official optional MCP server are present.
- Aegis has per-project statuses, issue hierarchy, dependencies, sprints, labels,
  saved filters, activity CSV export, idempotent POST replay, and agent delegation.
- Mentor has page versions, labels, comments, attachments, backlinks, and visibility.
- Auth includes local sessions, scoped bearer tokens, and OIDC login/provisioning.
- Operations include `athena-backup`, `athena-restore`, and `athena-doctor`.
- Run work includes `X-Athena-Run`, parent-run and fork headers, `/events?run_id=`,
  run lineage pages, a fork contract endpoint, and a documented determinism contract.

---

## 3. Jira/Confluence Product Anatomy

This is a neutral teardown of the *problem space* and primitives — not a blueprint
to copy. Sourced from Atlassian's own documentation plus the analyses cited in §4.
**[Fact + Interpretation]**

### 3.1 Jira — core primitives
- **Issue** is the atom: a typed record (issue *type*: epic, story, task, bug,
  sub-task, plus custom types) with a status, priority, assignee/reporter, labels,
  components, fix/affects **versions**, and an arbitrary set of **custom fields**.
- **Hierarchy:** Epic → Story/Task → Sub-task (and, in Premium, arbitrary levels
  above epic). This hierarchy drives roadmaps and rollups.
- **Project** is the container; **company-managed** projects share org-wide schemes
  (workflow, fields, permissions) while **team-managed** projects keep their config
  local — Atlassian's own admission that scheme-sharing was too heavy for small teams.
- **Workflow** is a state machine: statuses + **transitions**, each transition
  optionally guarded by **conditions**, **validators**, and **post-functions**
  (side effects). This is Jira's deepest moat and its heaviest complexity.
- **Boards** (Scrum/Kanban) sit on top of a **backlog** and **sprints**; swimlanes,
  WIP limits, quick filters. A board is a *view*, not where the data lives.
- **JQL** (Jira Query Language) is a real query language over every field, powering
  saved **filters**, **dashboards**, and automation. Power users live in JQL.
- **Automation** rules (no-code when/if/then) and **screens/screen schemes** control
  which fields appear when.

### 3.2 Confluence — core primitives
- **Space** (global or personal) is the container; **pages** form a tree.
- **Page** has version history, inline + footer **comments**, **restrictions**
  (view/edit at the page level), **labels**, **attachments**, and **page properties**.
- **Templates / blueprints** seed structured pages; **macros** embed dynamic content
  (Jira issues, tables of contents, status badges, include-page, etc.). The macro/
  blueprint surface is large and is a frequent source of editor pain.
- Newer surfaces: **databases**, **whiteboards**, **smart links**. These widen the
  product well beyond "a wiki."
- The Jira↔Confluence link (embed issues in a page, link pages to issues, "what
  pages mention this issue") is the suite's signature value — and is exactly what
  Athena gets *for free* by sharing one database.

### 3.3 Information architecture, permissions, admin
- **Permissions are layered and scheme-based.** Jira: global permissions → project
  permission *schemes* → project *roles* → optional **issue security schemes** (row-
  level). Confluence: global → **space permissions** → **page restrictions**.
  Powerful, and notoriously hard to reason about ("why can this person see this?").
- **Org/site admin:** user provisioning via **SCIM**, **SSO** (SAML/OIDC) through
  Atlassian Guard/Access (a paid add-on), audit logs, IP allowlisting, data
  residency. These are real enterprise requirements — and mostly irrelevant to a
  single-operator self-hosted box.
- **Migration/import-export:** Jira CSV import; full XML/JSON site backups; Confluence
  space export to XML/HTML/PDF; the Cloud Migration Assistants. Getting data *out*
  cleanly is a recurring lock-in complaint.
- **Marketplace:** the Connect/Forge app platform (ScriptRunner, Tempo, BigPicture,
  Structure, etc.) with revenue share. The ecosystem is a genuine moat — *and* its
  existence advertises gaps in the core product (time tracking, scripting, advanced
  hierarchy were long missing natively).

### 3.4 What this anatomy tells Athena **[Interpretation]**
The valuable, copy-worthy ideas are *structural and small*: typed cross-links,
a real (but simple) query language, saved filters, version history, an audit
trail, and the issues↔docs join. The dangerous, reject-worthy ideas are the
*configurability surfaces*: workflow conditions/validators/post-functions, screen
schemes, permission/issue-security schemes, blueprint/macro sprawl, and a plugin
platform. Jira/Confluence are powerful because they are configurable; they are
hated because they are configurable. Athena's edge is to pick sane defaults and
refuse most of the knobs.

---

## 4. Atlassian Research Findings

### 4.1 Why Jira & Confluence became dominant **[Interpretation, widely reported]**
- **Product-led, bottoms-up distribution** with historically no traditional
  enterprise sales team: cheap entry, land-and-expand, a self-serve Marketplace.
- **Bundling**: Jira + Confluence + (later) Bitbucket/Trello/JSM cover the whole
  software lifecycle, with cross-product links as the glue.
- **Depth + ecosystem**: workflow customization and 1,000s of Marketplace apps make
  Jira "the tool you can't be fired for choosing" in large orgs.

### 4.2 Pricing & packaging **[Fact]**
- Jira Cloud: Free (≤10 users) → **Standard ≈ $7–8.15/user/mo** → **Premium ≈
  $14–16/user/mo** → Enterprise (custom). Confluence Cloud: **Standard ≈ $5–6** →
  **Premium ≈ $10–11** → Enterprise (custom). Most enterprises buy both, bundled.
  ([Jira pricing](https://www.atlassian.com/software/jira/pricing),
  [Confluence pricing](https://www.atlassian.com/software/confluence/pricing),
  [Software Pricing Guide 2025](https://softwarepricingguide.com/atlassian-jira-pricing-2025-every-plan-the-data-center-vs-cloud-cost-decision-and-the-price-increases-nobody-warned-you-about/))
- **Data Center** is now the only self-managed option: annual, user-tier priced,
  with the entry tier raised to a **500-user minimum** — i.e., a small shop can no
  longer buy a cheap perpetual self-hosted license. ([DC end-of-life](https://www.atlassian.com/licensing/data-center-end-of-life))
- **TCO balloons** from per-seat pricing × two products × Guard/Access add-on ×
  paid Marketplace apps (often billed per the same user tier). **[Interpretation]**

### 4.3 The Server end-of-life forcing function **[Fact]**
Atlassian ended support for **Server** products on **February 15, 2024** — no
security patches, no support — pushing customers to Cloud (per-seat) or Data
Center (500-user-minimum). ([Farewell to Server](https://www.atlassian.com/blog/announcements/farewell-to-server),
[30-day countdown](https://www.atlassian.com/blog/announcements/server-support-30-day-countdown))
**[Interpretation]** This stranded exactly the small, cost-sensitive, self-hosting
segment that Athena targets — the clearest single tailwind for a self-owned
alternative.

### 4.4 Layoffs & the AI pivot **[Fact]**
- **March 2023:** ~500 roles (~5%), redeploying toward cloud/ITSM.
  ([Atlassian, Mar 2023](https://www.atlassian.com/blog/announcements/atlassian-team-update-march-2023))
- **2025:** ~150 support roles.
- **March 2026:** **~1,600 roles (~10%)**, framed as self-funding AI + enterprise
  sales after an AI-driven stock decline; ~40% of cuts in North America.
  ([CNBC, Mar 2026](https://www.cnbc.com/2026/03/11/atlassian-slashes-10percent-of-workforce-to-self-fund-investments-in-ai.html),
  [Atlassian team update Mar 2026](https://www.atlassian.com/blog/company-news/atlassian-team-update-march-2026))
**[Interpretation]** Atlassian is reallocating toward AI under margin pressure. That
is reassuring for a lean challenger (the incumbent is distracted and cost-cutting)
and a caution (they are pouring resources into Rovo agents — see §4.6).

### 4.5 What users love / hate **[Fact: recurring themes; Interpretation: weighting]**
The most-corroborated complaints across HN/Reddit/blogs:
- **Slow.** "One *huge* issue: it's slow"; multi-second page loads compound across
  dozens of daily interactions. ([HN](https://news.ycombinator.com/item?id=25594451),
  [HN](https://news.ycombinator.com/item?id=25358403))
- **Over-engineered / complex.** "Over-engineered, unnecessarily complex." Admins
  bury developers in custom fields and workflow ceremony; "optimizing for Jira
  instead of for building software." ([HN](https://news.ycombinator.com/item?id=23804620),
  [HN: Slow death of Agile & Jira](https://news.ycombinator.com/item?id=41659128))
- **Poor search** ("a black hole"), **notification noise**, and **Confluence editor
  frustration** recur. ([HN: Why Jira sucks](https://news.ycombinator.com/item?id=25590846))

Where Jira/Confluence are **genuinely hard to beat**: deep workflow customization,
enterprise governance (schemes, audit, SSO/SCIM, residency), the Marketplace
ecosystem, scale/reliability, JQL's expressive power, and integration breadth.
**[Interpretation]** Athena should not try to win on any of those; it should win
where they are weak (speed, simplicity, self-hosting cost, data ownership,
agent-native APIs).

### 4.6 AI direction: Rovo **[Fact]**
Atlassian **Rovo** (Search + Chat + **Agents**) reached broad availability across
paid Cloud plans through 2025 (Premium/Enterprise Apr–Jul 2025; Standard Oct 2025),
bundled at no extra upfront cost. ([Rovo](https://www.atlassian.com/software/rovo),
[Rovo agents](https://support.atlassian.com/rovo/docs/agents/))
**[Interpretation]** This is AI *added to* a 20-year-old product model — assistants
and agents operating on top of the existing UI/permissions. Athena's opening is the
inverse: a system whose primitives, API, and audit trail were designed for agents
as first-class actors from day one.

---

## 5. Video / Insider Source Review

The brief specifically asked about a viral video of a laid-off Atlassian developer.
It exists and is corroborated — but its content matters, and it is treated here as
**one anecdote**, not as representative data.

| Field | Detail |
|---|---|
| **Title** | "I was laid off by Atlassian" (long-form YouTube walkthrough) |
| **URL** | https://www.youtube.com/watch?v=55pTFVoclvE |
| **Author/speaker** | **Vasilios Syrakis**, senior/edge-infrastructure engineer, ~8 years at Atlassian |
| **Date** | Laid off **March 12, 2026**; video circulated **May 2026** |
| **Reach** | Reported **1.1M+ views in ~8 days** |
| **Key claims** | Walks through the **edge infrastructure** he built: ~2,000 proxy servers across 13 AWS regions, traffic routing, provisioning automation, proxies, authentication, scaling — the "plumbing" behind Jira, Confluence, and Bitbucket. Frames it as reflection after an AI-justified layoff. |
| **Relevance to Athena** | **Low–moderate, thematic only.** It is about *infrastructure*, not Jira/Confluence product design, IA, or workflows. It corroborates the **March 2026 ~10% layoff** and the "cut strong engineers to fund AI" narrative, and (via commentary) the meme that *execution is now cheap* — but it is **not** evidence about the product's internals. |
| **Reliability** | **8/10** as a primary first-person source on Atlassian infra & the layoff; multiple independent outlets corroborate the person, date, and reach. |

Corroborating sources: [GreekReporter (May 19, 2026)](https://greekreporter.com/2026/05/19/greek-engineer-laid-off-atlassian-reveals-infrastructure-software-giant/) ·
[Threads/@carnage4life commentary](https://www.threads.com/@carnage4life/post/DYe32q-FGIU/) ·
[Medium write-ups](https://medium.com/data-science-in-your-pocket/fired-for-ai-engineer-exposed-atlassian-infrastructure-c327181c6a34).

**Disambiguation [Fact]:** This is **not** the also-viral Cloudflare layoff video
(Brittany Pietsch, Jan 2024) and not a generic tech-layoff TikTok — a real risk
the brief flagged.

**Other insider/expert signal** (more directly relevant than the video): the
long-running Hacker News threads in §4.5 are the better corroborated "insider"
record of *product* sentiment. Reliability of any single HN comment is low (3–5/10),
but the **consistency of the slow/over-engineered theme across years and threads**
raises the aggregate signal. **[Interpretation]**

> **Discipline note (per brief):** one viral video is anecdotal. The defensible,
> corroborated takeaways are the *macro facts* (layoffs, Server EOL, pricing, AI
> pivot) and the *aggregate* developer-sentiment themes — not any single creator's
> framing.

---

## 6. Competitor Matrix

Self-hostability and license are decisive for Athena's positioning, so they lead.
**[Fact]** for license/self-host/pricing; **[Interpretation]** for lessons.

| Product | Category | Self-host | License | Athena should COPY | Athena should REJECT |
|---|---|---|---|---|---|
| **Linear** | Issues | No (SaaS) | Proprietary | Speed obsession; opinionated defaults; keyboard-first; clean API + [MCP/agents](https://linear.app/integrations/notion-agent); triage inbox; cycles | Closed/no self-host; mandatory cloud |
| **Notion** | Docs+DB+wiki | No (SaaS) | Proprietary | Block model flexibility; templates; strong [MCP server](https://github.com/makenotion/notion-mcp-server) & "data sources" API | All-in-one sprawl; performance at scale; no self-host |
| **GitHub Issues/Projects** | Issues | Via GH Enterprise Server | Proprietary | Markdown everywhere; cross-refs (`#123`); Projects v2 fields/views; GraphQL; tight code linkage | Issues are thin without Projects; config split is confusing |
| **YouTrack (JetBrains)** | Issues+KB | Yes | Proprietary (free <10) | Powerful query language done *ergonomically*; command palette; built-in KB | Proprietary; can feel power-user-only |
| **ClickUp** | All-in-one PM | No | Proprietary | Multiple views over one dataset | Feature bloat (the canonical anti-pattern); performance complaints |
| **Asana** | PM | No | Proprietary | Rules/automation UX; portfolios/goals | No self-host; per-seat |
| **Trello** | Kanban | No | Proprietary (Atlassian) | Radical simplicity; Butler automation | Too shallow for engineering work |
| **Basecamp** | PM+msg | No (cloud) | Proprietary | **Anti-bloat philosophy**; flat pricing; opinionated scope | Not for issue-tracking depth; no self-host |
| **Obsidian** | KB (local) | Local files | Proprietary (free) | **Local-first**; wikilinks/backlinks/graph; plain-files portability | No multi-user server; no API-first model |
| **Outline** | Team wiki | Yes | **BSL** (source-available) | Clean editor; search; API; structured collections | BSL competitive restriction; not OSI-open |
| **BookStack** | Wiki | Yes | **MIT** | Simple shelves/books/chapters; easy ops; real OSS | PHP/LAMP stack; limited API/agent story |
| **Plane** | Issues+cycles+docs | Yes | **AGPL-3.0** (community); paid commercial edition | Cycles/modules; webhooks+REST in OSS; Docker simplicity ([editions](https://developers.plane.so/self-hosting/editions-and-versions)) | Open-core gating (SSO/epics/workflows are paid); heavier stack (Django+Next) |
| **OpenProject** | PM+wiki | Yes | **GPL/AGPL** (Community free) | Mature self-host; work packages; OIDC; genuinely-free community edition; [XWiki partnership](https://www.openproject.org/blog/open-source-jira-confluence-alternative/) for docs | Ruby/enterprise heft; classic-PM feel; two systems stitched together |
| **Huly** | All-in-one (Jira+Notion+Linear+Slack) | Yes | OSS | Integrated suite vision; one place for issues+docs+chat | Very broad scope; young; ops complexity |
| *(also surveyed)* | | | | Redmine (GPL, mature/dated), Taiga (AGPL), XWiki (LGPL), Focalboard (Mattermost-discontinued — a caution), Wiki.js (AGPL) | |

**Reading the matrix [Interpretation]:** The SaaS leaders (Linear, Notion) define
the *quality bar* (speed, API/MCP, opinionated defaults) but cannot be self-hosted.
The self-hostable OSS tools are *split* — Plane/OpenProject for work, Outline/
BookStack/XWiki for docs — and the strongest "combined" answer in the market is a
*partnership of two systems* (OpenProject + XWiki). **Athena's whitespace is a
single, integrated, self-hosted system with one database, one audit log, one
search, native cross-links, AND a first-class agent surface** — a combination no
competitor currently offers in one lightweight package.

---

## 7. Strategic Product Thesis for Athena

**Positioning (one sentence):** *Athena is the smallest system a solo operator and
an AI fleet can run forever — Jira-and-Confluence-shaped work and knowledge in one
auditable SQLite file, with an API built for agents first.* **[Recommendation]**

### 7.1 What Athena should COPY
- The **structural** wins from Jira/Confluence: typed cross-links (have it), version
  history (have it), an audit trail (have it), a **simple** saved-filter/query
  capability (don't have it), issue **hierarchy** (epic/parent — don't have it).
- From Linear/Notion: **speed and opinionated defaults**; a clean, documented API;
  an **MCP server** and **webhooks** so agents are first-class.
- From GitHub: **Markdown everywhere** and frictionless cross-references.
- From Basecamp/Obsidian: an explicit **anti-bloat** stance and **local-first /
  portability** as a feature, not an afterthought.

### 7.2 What Athena should REJECT (be blunt)
- Jira's **workflow engine** (conditions/validators/post-functions), **screen
  schemes**, **permission/issue-security schemes**, and the **custom-field type zoo**.
- Confluence's **macro/blueprint** surface, **whiteboards**, and **databases**.
- A **Marketplace/plugin platform** (Connect/Forge). It is a security and maintenance
  sinkhole; replace it with a great open API + MCP + webhooks.
- **Multi-tenancy / org-site hierarchy / per-seat billing machinery** — Athena is
  single-tenant and self-hosted by design.
- **Real-time collaborative editing** (OT/CRDT) for now — version history + optimistic
  concurrency is enough; collab editing is a multi-quarter rabbit hole.
- A **heavyweight SPA / JS build chain** — HTMX is a feature, keep it.

### 7.3 What Athena should SIMPLIFY (the sharp middle path)
- **Workflow:** allow a *per-project ordered list of statuses* (and category:
  todo/doing/done for boards). No transition guards. This buys flexibility without
  Jira's complexity.
- **Permissions:** add **read authorization** and a **coarse per-space/project
  visibility** (private / internal / public) plus optional membership — *not* a
  scheme system.
- **Fields:** a *tiny* set of optional typed fields per project (e.g. a few
  select/number/date fields), capped hard. Resist the zoo.

### 7.4 What Athena can uniquely do (self-hosted + agent-native)
- **One file, one truth, one audit log.** Backups are `cp athena.db`. Every agent
  and human action is in `activity`. No "which system is right?" split-brain.
- **Agent-native surface:** scoped tokens (have it) + **webhooks/event feed** +
  **official MCP server** + **idempotent bulk API** + cursor pagination + OpenAPI
  (FastAPI already generates `/openapi.json`). Agents can *subscribe, act
  idempotently, and be audited* — the thing Rovo bolts on, Athena has by design.
- **Cross-module intelligence:** issues and docs already share search + links;
  extend to "docs that mention this issue," "stale docs," "issues blocking this epic"
  as cheap SQL, not a plugin.

### 7.5 Build order principle
**Substance before polish.** Until reads are authorizable, bodies are Markdown,
files can be attached, and agents can subscribe to events, additional UI styling is
premature. §10–§12 sequence this.

---

## 8. Architecture Gaps

Ordered by how much they still block "serious, multi-actor, agent-native" after
the June 30 merge wave. **[Interpretation over Fact]**

1. **Portability is coarse.** Whole-DB backup/restore exists, but there is no
   selective per-project/per-space JSON export or dry-run import. This is now the
   biggest migration and data-ownership blocker.
2. **Replay bundles are incomplete.** The event feed, run lineage, and fork
   contract exist; operators still need a single replay manifest/bundle that
   freezes one run's replay-safe facts plus lineage metadata for handoff and audit.
3. **Agent administration is basic.** Agent users can act through tokens and be
   delegated issues, but there is no dedicated admin/policy layer for allowed
   projects/spaces, delegation constraints, or agent roster review.
4. **API safety is partial.** Idempotent POST replay exists. Remaining agent
   ergonomics are per-token rate limiting, bulk endpoints, cursor coverage on
   list endpoints that still lack it, and ETag/`If-Match` concurrency.
5. **Packaging/retention is still manual.** `athena-doctor` validates deploy
   prerequisites, but a production install path and retained/off-host backup
   helper would strengthen Athena's "simple to self-host" moat.
6. **Search can still improve without new infrastructure.** FTS5 is enough, but
   phrase/field search, ranking tuning, and result pagination in `/find` remain
   useful. Do **not** add Elasticsearch.

---

## 9. Security / Operations Gaps

**[Fact, from code]** unless marked.

- **Authorization exists but is public-by-default.** Project/space visibility and
  membership now gate reads, but new containers default to public and there is no
  global "all reads require auth" deployment mode. This is acceptable for local/
  tailnet dogfood, but should be a deliberate hosting decision.
- **No per-token rate limiting** — an agent loop can still hammer the service.
  `Idempotency-Key` replay exists for POST retry safety, but rate limits remain open.
- **OIDC exists; SAML/SCIM do not.** Basic OIDC login/provisioning closes the near-
  term SSO gap. SAML and SCIM remain deferred enterprise items.
- **Secrets & transport** are handled sensibly (env, HttpOnly cookies, CSRF, CSP,
  `ATHENA_COOKIE_SECURE`, body limits). Good baseline. **[Fact]**
- **Audit coverage is strong but read-blind:** writes are well-recorded; there is no
  record of *reads/exports* (often required in regulated contexts). Lower priority.
- **Backups are still operator-driven.** `athena-backup`, `athena-restore`, and
  `athena-doctor` exist; scheduled/retained/off-host backup automation is still
  documentation or helper-script work.
- **Attachment security is implemented but should stay watched.** Files live under
  a configured directory with randomized stored names and authenticated downloads;
  future import/export work must preserve that invariant.
- **Markdown sanitization is implemented.** Keep render-on-read, raw HTML disabled,
  `nh3` sanitization, and the strict CSP as non-negotiable invariants.

---

## 10. Recommended Roadmap

Six themes, sequenced so each unblocks the next. Each maps to several §11 slices.
**[Recommendation]**

- **Theme A — Data ownership and migration (now).** Per-space/project JSON export,
  then dry-run import, then source-specific importers.
- **Theme B — Replayable agent substrate (now).** Package run replay manifests and
  make log/fork contracts easy to hand off between agents.
- **Theme C — Agent administration (now/next).** Turn agent users, delegation,
  scopes, and project/space access into an inspectable admin workflow.
- **Theme D — API safety and scale (next).** Rate limits, bulk operations, ETags,
  cursor coverage, and search pagination.
- **Theme E — Self-host packaging (next).** Retained/off-host backup helper,
  documented systemd/env layout, and a one-command/few-command install path.
- **Theme F — Product polish (later).** Dashboards/reporting, richer query-lite,
  and UX refinements after the data/replay/admin foundation is solid.

**Deferred / risky / over-scoped:** SAML/SCIM; real-time collaborative editing;
burndown analytics; custom-field zoo; a plugin platform; mobile apps; any
non-SQLite search backend. Revisit only on concrete demand.

---

## 11. Ranked PR Slice Backlog

Each slice is sized for a small PR and follows `AGENTS.md` (stay in lane, no stray
data stores, real tests, runs against the real DB). **Score = impact × leverage ÷
risk** (1–10). Files reference real modules.

> **Conventions:** *Size* S≈<150 LOC, M≈150–400, L≈400+. *Risk* = blast radius +
> reversibility.

> **2026-06-30 status note:** This ranked backlog is preserved because it explains
> the original build sequence. The following items have V1 implementations on
> `main`: S1, S2, S3, S4, S5, S6, S7, S8, S9, S12, and S15's OIDC subset. Treat S10
> plus the new replay/agent-admin/API-safety follow-ups in §12 as the live queue.

### S1 — Markdown rendering + server-side sanitization · **Score 9**
- **Why:** Plain-text bodies are the single biggest credibility gap for Mentor and
  hurt Aegis. Pure presentation change, no schema, immediately visible.
- **User story:** *As a writer, I want headings/lists/code/tables/links in pages and
  issue descriptions so docs are actually usable.*
- **Files:** `web/render.py` (render pipeline), `templates/*` (where bodies render),
  `pyproject.toml` (+`markdown-it-py`/`markdown` + a sanitizer e.g. `nh3`/`bleach`).
- **Data model:** none now; optionally add `body_format` later (default `markdown`).
- **API:** none — API keeps returning raw body; rendering stays web-only.
- **UI:** render Markdown→sanitized HTML; preserve `[[ref]]` cross-links (run the
  link pass over rendered output); add a short formatting hint in editors.
- **Tests:** XSS injection is inert; cross-links still resolve inside Markdown;
  code blocks/tables render; broken refs still show.
- **Risk:** Medium (XSS) — mitigated by mandatory sanitizer + existing strict CSP.
- **Size:** M · **Deps:** none · **DoD:** ruff+pytest green; app boots; a page with
  headings/code/a `[[issue:1]]` renders correctly and a `<script>` body is inert.

### S2 — Attachments on issues and pages · **Score 9**
- **Why:** Table stakes; both modules need files; referenced in ARCHITECTURE, unbuilt.
- **User story:** *As a user, I want to attach files to an issue or page and download
  them later.*
- **Files:** new `core/attachments.py`; new migration `0021_attachments.sql`;
  `aegis/api.py` + `mentor/api.py` (+sub-resource endpoints); `web/router.py` +
  `web/mentor.py` (upload forms, download routes); `config.py` (`ATHENA_ATTACH_DIR`,
  size cap).
- **Data model:** `attachments(id, target_kind, target_id, filename, content_type,
  byte_size, sha256, stored_name, uploaded_by, created_at)`; index `(target_kind,target_id)`.
- **API:** `POST /issues/{id}/attachments`, `POST /pages/{id}/attachments` (multipart),
  `GET /attachments/{id}` (stream), `DELETE /attachments/{id}`; record in `activity`.
- **UI:** upload control + attachment list with download links on issue/page detail.
- **Tests:** upload→list→download round-trips; size cap → 413; type allowlist;
  path-traversal filename is neutralized; delete removes row + blob; audit recorded.
- **Risk:** Medium (file handling) · **Size:** L · **Deps:** none ·
- **DoD:** files stored outside web root with randomized names; caps enforced;
  green gate; manual upload/download verified.

### S3 — Outbound event feed + webhooks · **Score 9**
- **Why:** The agent-native spine. Turns the audit log into a subscribable stream;
  unblocks notifications (S8) and MCP (S5). Agents stop polling.
- **User story:** *As an agent/integration, I want to receive events (or read an
  event cursor) when issues/pages change, so I can react.*
- **Files:** new `core/events.py` (emit from `activity.record`), new `core/webhooks.py`,
  migration `0022_webhooks.sql`; `core/activity_api.py` or new `events_api.py`
  (`GET /events?after=<id>`); webhook CRUD router; `config.py`.
- **Data model:** reuse `activity` as the event log; add `webhooks(id, url, secret,
  event_filter, active, created_by, created_at)` and `webhook_deliveries(...)` for
  retry/audit.
- **API:** `GET /events` (cursor over activity, scoped by token); `POST/GET/DELETE
  /webhooks` (admin scope); HMAC-signed delivery with retry/backoff.
- **UI:** minimal admin page to register/inspect webhooks (can defer to API-only first).
- **Tests:** event appears in `/events` after a write; webhook fires with valid HMAC;
  failed delivery retries and is recorded; token scope filters events.
- **Risk:** Medium (delivery reliability; SSRF on webhook URLs — validate/allowlist).
- **Size:** L · **Deps:** none (but pairs with S1.5 read-authz for scoping) ·
- **DoD:** create an issue → event observable via `/events` and a test webhook
  receiver; signatures verify; green gate.

### S4 — Read authorization + visibility (foundation) · **Score 8**
- **Why:** Closes the public-read hole; precondition for any exposed/multi-actor
  use and for scoping events/search. *Product decision — see Open Questions.*
- **User story:** *As an operator, I want reads to require auth (optionally) and
  spaces/projects to be private/internal/public, so confidential work isn't public.*
- **Files:** `core/identity.py` (`read_actor`/`optional_read_actor`); `config.py`
  (`ATHENA_REQUIRE_AUTH_READS`); apply to GET routes in `aegis/api.py`,
  `mentor/api.py`, `core/search_api.py`, `web/router.py`, `web/mentor.py`; migration
  for `visibility` columns on `projects`/`spaces`.
- **Data model:** `projects.visibility` / `spaces.visibility` (`private|internal|
  public`, default `internal`); later a `memberships` table.
- **API:** GET endpoints honor the config gate and visibility; search/event results
  filtered to what the actor may read.
- **UI:** visibility selector on project/space edit; sign-in prompt on gated reads.
- **Tests:** with gate on, anonymous GET → 401; viewer sees `internal/public` only;
  private hidden from non-members; search/activity respect visibility.
- **Risk:** Medium-High (touches every read; behavior change) · **Size:** L ·
- **Deps:** none, but should land before exposing events/search externally ·
- **DoD:** gate defaults preserve current local-dev behavior; exposed mode enforces
  authz; green gate; manual check of each persona.

### S5 — Official MCP server for Athena · **Score 8**
- **Why:** The signature differentiator — make Athena a first-class tool for AI
  fleets (issues/docs/search/links as MCP tools), the inverse of "AI bolted on."
- **User story:** *As an agent, I want to file/triage issues and read/write docs in
  Athena through MCP with a scoped token.*
- **Files:** new `src/athena/mcp/` server wrapping the existing REST/data layer;
  `pyproject.toml` script entry; docs.
- **Data model:** none (reuses tokens/scopes).
- **API:** MCP tools mapping to existing endpoints (search, create/triage issue,
  read/write page, resolve links); honors token scopes + (S4) visibility.
- **UI:** none (docs + example client config).
- **Tests:** MCP tool calls authenticate via token scope; writes are audited;
  scope/visibility enforced.
- **Risk:** Medium · **Size:** M-L · **Deps:** stable API (have it), ideally S3/S4 ·
- **DoD:** a Claude/agent client can search and create an issue via MCP against a
  real DB; actions appear in `activity`; green gate.

### S6 — Per-project status sets (lean workflow) · **Score 7**
- **Why:** Removes the rigid global 3-state lifecycle without importing Jira's
  workflow-engine complexity.
- **User story:** *As a project lead, I want to define my project's statuses (e.g.
  Backlog/Todo/Doing/Review/Done) and have boards reflect them.*
- **Files:** migration (`project_statuses` or `projects.statuses` JSON + `category`);
  `aegis/issues.py` (validate status against project's set; default set for backlog),
  `aegis/projects.py`, `aegis/api.py`, `web/router.py` (boards group by project set).
- **Data model:** `project_statuses(project_id, name, category, position)`; backfill
  existing projects with `open/in_progress/done`; backlog (no project) keeps the
  global default.
- **API:** status CRUD per project; issue create/update validate against the set.
- **UI:** status management on project edit; board columns from the project's set.
- **Tests:** new project gets defaults; custom set validates; invalid status → 422;
  boards render per-project columns; moving an issue between projects maps status.
- **Risk:** Medium-High (touches the core lifecycle + boards + migration) · **Size:** L ·
- **Deps:** none · **DoD:** existing data unchanged; custom statuses work end-to-end;
  green gate. **Explicitly out of scope:** transitions, conditions, validators.

### S7 — Issue hierarchy (parent / epic-lite) · **Score 7**
- **Why:** Enables planning/rollups (epic → children) with one column, no scheme zoo.
- **User story:** *As a planner, I want to nest issues under a parent/epic and see
  children + progress.*
- **Files:** migration (`issues.parent_id` + guards), `aegis/issues.py` (cycle/self
  guards, same-nothing constraints), `aegis/api.py`, `web/router.py`, templates.
- **Data model:** `issues.parent_id INTEGER REFERENCES issues(id)`; index it.
- **API:** set/clear parent; list children; include child rollup counts.
- **UI:** parent picker; children list + done-count on detail.
- **Tests:** set/clear parent; reject self/cycle; children listed; rollup correct;
  delete-with-children policy mirrors page/space (refuse or reparent).
- **Risk:** Medium · **Size:** M · **Deps:** none · **DoD:** green gate; nesting
  works; cross-link `[[issue:N]]` still fine.

### S8 — Notifications: watching + @mentions + inbox · **Score 7**
- **Why:** Humans need to know they were mentioned/assigned; closes a top Jira
  complaint (noise) by being *quiet and targeted* from the start.
- **User story:** *As a user, I want an inbox of things I'm watching or was mentioned
  in, without email spam.*
- **Files:** new `core/notifications.py`; migrations (`watches`, `notifications`);
  parse `@user` (and reuse `[[ ]]`) on write; `web` inbox page; consume S3 events.
- **Data model:** `watches(user_id, target_kind, target_id)`; `notifications(id,
  user_id, event_id, read_at)`.
- **API:** list/mark-read notifications; watch/unwatch a target.
- **UI:** inbox + unread badge; auto-watch on comment/assign; @mention autocomplete.
- **Tests:** assignment/mention creates a notification for the right user only;
  mark-read works; no self-notify; watching a target delivers its events.
- **Risk:** Medium · **Size:** L · **Deps:** **S3** (event feed) · **DoD:** in-app
  inbox works against real DB; green gate. (Email delivery deferred.)

### S9 — Saved filters + query-lite for issues · **Score 6**
- **Why:** Agents and humans need composable queries and reusable views (a lean,
  *deliberately small* JQL-equivalent).
- **User story:** *As a user/agent, I want to query "status=open AND assignee=me AND
  label=bug" and save it as a view.*
- **Files:** `aegis/issues.py` (extend `list_issues`: assignee, priority, multi-label,
  parent), a small safe parser, migration `saved_filters`, `aegis/api.py`,
  `web/router.py`.
- **Data model:** `saved_filters(id, owner_id, name, query, shared)`.
- **API:** `GET /issues?q=...`; CRUD saved filters.
- **UI:** filter bar + save/load views.
- **Tests:** parser rejects junk safely (no SQL injection); each field filters;
  saved filter round-trips; param stays parameterized.
- **Risk:** Medium (query parsing) · **Size:** M · **Deps:** none (better after S6/S7) ·
- **DoD:** green gate; documented mini-grammar; agent can query via API.

### S10 — Per-space/project JSON export + import (portability) · **Score 6**
- **Why:** Data ownership is a core promise; enables migration in/out and selective
  backup. Counters lock-in (a top Atlassian grievance).
- **User story:** *As an operator, I want to export a space or project (pages/issues +
  versions + links + attachments manifest) to JSON and re-import it.*
- **Files:** new `core/portability.py`; `ops.py` CLI (`athena-export`/`athena-import`);
  optional API endpoints; reuse existing data layers.
- **Data model:** none (read/write through existing tables; preserve ids/links where safe).
- **API/CLI:** export to a JSON bundle; dry-run import with conflict report.
- **UI:** optional later.
- **Tests:** round-trip a space and a project (incl. versions + cross-links); dry-run
  reports conflicts; import is idempotent on re-run.
- **Risk:** Medium · **Size:** M-L · **Deps:** S2 for attachment payloads ·
- **DoD:** export→import reproduces content + links; green gate.

### S11 — Page comments · **Score 5**
- **Why:** Confluence-parity for discussion on docs; cheap given the issue-comment pattern.
- **Files:** migration (`page_comments`, or generalize `comments` to polymorphic),
  `mentor/api.py`, `web/mentor.py`, templates.
- **Data/API/UI/Tests:** mirror issue comments (create/edit/delete by author; audit).
- **Risk:** Low-Medium · **Size:** M · **Deps:** none · **DoD:** green gate; comment
  on a page works; audited.

### S12 — API ergonomics: idempotency keys + cursor pagination + ETags · **Score 6**
- **Why:** Agents need safe retries and stable paging; humans need fast lists. Small,
  broad-benefit hardening.
- **Files:** `aegis/api.py`, `mentor/api.py`, `core/*_api.py`, a small middleware/util.
- **Data model:** `idempotency_keys(key, actor_id, response_hash, created_at)` (TTL).
- **API:** honor `Idempotency-Key` on POST; cursor (`?after=`) on list endpoints;
  `ETag`/`If-Match` for optimistic concurrency on issue/page updates.
- **Tests:** replayed POST with same key returns the first result (no dup); cursor
  paging is stable; stale `If-Match` → 412.
- **Risk:** Low-Medium · **Size:** M · **Deps:** none · **DoD:** green gate; documented.

### S13 — Bulk operations API for agents · **Score 5**
- **Why:** Agents triage in batches; N single calls are slow and noisy in the audit log.
- **Files:** `aegis/api.py` (bulk create/transition/label), keep per-row audit.
- **API:** `POST /issues/bulk` etc., partial-success report.
- **Tests:** bulk create/transition; partial failures reported; each row audited.
- **Risk:** Low-Medium · **Size:** M · **Deps:** S12 (idempotency) · **DoD:** green gate.

### S14 — Search quality: phrase/field search + paginated `/find` · **Score 5**
- **Why:** Search is a named Jira weakness and a cheap Athena win on FTS5.
- **Files:** `core/search.py` (phrase + `kind:`/`status:` field filters), `core/search_api.py`,
  `web/router.py` find view (pagination + filters).
- **Tests:** phrase match; field filter; pagination; ranking sanity; injection-safe.
- **Risk:** Low-Medium · **Size:** M · **Deps:** none (respect S4 visibility) · **DoD:** green gate.

### S15 — OIDC SSO (deferred enterprise) · **Score 4**
- **Why:** Only needed if humans-at-an-org adopt Athena; not for solo+fleet. Listed
  for completeness; **defer** until demand.
- **Risk:** Medium · **Size:** L · **Deps:** S4 · **DoD:** out of near-term scope.

> **Explicitly NOT in the backlog (over-scoped / rejected):** workflow
> conditions/validators/post-functions; screen schemes; permission/issue-security
> schemes; custom-field type zoo; Confluence macros/blueprints/whiteboards/databases;
> a plugin marketplace; multi-tenancy; real-time collaborative editing; non-SQLite
> search backend; native mobile apps.

---

## 12. Top 5 Immediate Next Tasks

Chosen after reconciling the June 26 backlog against current `main` on 2026-06-30.

1. **S10a — Export-only portability V1.** Add `athena-export` for one project or
   space, producing a stable JSON bundle with content, versions/history, labels,
   links, and an attachment manifest. Keep import out of the first PR.
2. **S10b — Dry-run import.** Read the V1 bundle, report conflicts/missing actors/
   missing attachment blobs, and prove idempotent planning before any writes.
3. **Run replay manifest.** Add an endpoint/CLI that emits one run's ordered
   replay-safe events, parent/fork coordinates, and determinism metadata as a
   portable handoff/audit artifact.
4. **Agent administration V2.** Add an admin-facing way to review agent users,
   token scopes, project/space access, and delegation policy. Do not build a
   workflow engine.
5. **API safety follow-up.** Add per-token rate limiting first, then bulk endpoints
   and ETag/`If-Match` where update races matter.

*Immediate follow-ons:* backup retention/off-host guidance, one-command packaging,
and `/find` pagination/field search.

---

## 13. Open Questions (need a product decision)

These change *what* gets built; they're for Kevin, not for an agent to assume.

1. **Audience:** Is Athena strictly *solo operator + AI fleet on a tailnet*, or must
   it serve *multiple humans/teams* soon? This sets how much of the permission model
   (S4 + membership) to build now vs. defer.
2. **Reads:** Project/space visibility exists and defaults public. Should there be a
   deployment-wide `ATHENA_REQUIRE_AUTH_READS` mode, or is public-by-default on a
   trusted tailnet the intended long-term posture?
3. **Markdown:** Decided for now: render-on-read, CommonMark via `markdown-it-py`,
   raw HTML disabled, `nh3` sanitizer.
4. **Workflow depth:** Per-project status sets exist. Confirm we continue to reject
   transitions/guards/workflow schemes.
5. **Attachment storage:** Filesystem storage exists. Decide whether import/export
   bundles include blobs, an attachment manifest only, or both modes.
6. **Agent surface priority:** MCP, event feed, webhooks, delegation, lineage, and
   forking exist. Next choice is replay bundle first vs. agent-admin policy first.
7. **Migration sources:** What must Athena import first — ORACLE Markdown → Mentor,
   Notion tasks → Aegis, or Jira/Confluence exports? This orders S10's importers.
8. **Notifications channel:** In-app inbox only (recommended first), or email too?
9. **SSO/OIDC:** OIDC exists. Any near-term need for SAML/SCIM, or firmly deferred?
10. **Hosting timeline:** When (if) does Athena move off the laptop to the `flow`
    node? That date is what makes S4 (and rate limiting/backups) urgent vs. nice-to-have.

---

## Appendix — Source List (with reliability)

**Atlassian primary (9–10/10):**
[Jira pricing](https://www.atlassian.com/software/jira/pricing) ·
[Confluence pricing](https://www.atlassian.com/software/confluence/pricing) ·
[Farewell to Server](https://www.atlassian.com/blog/announcements/farewell-to-server) ·
[Server 30-day countdown](https://www.atlassian.com/blog/announcements/server-support-30-day-countdown) ·
[Data Center end-of-life](https://www.atlassian.com/licensing/data-center-end-of-life) ·
[Team update Mar 2023](https://www.atlassian.com/blog/announcements/atlassian-team-update-march-2023) ·
[Team update Mar 2026](https://www.atlassian.com/blog/company-news/atlassian-team-update-march-2026) ·
[Rovo](https://www.atlassian.com/software/rovo) · [Rovo agents](https://support.atlassian.com/rovo/docs/agents/)

**News / analyst (7–8/10):**
[CNBC — Atlassian cuts 10%/~1,600 (Mar 2026)](https://www.cnbc.com/2026/03/11/atlassian-slashes-10percent-of-workforce-to-self-fund-investments-in-ai.html) ·
[Software Pricing Guide — Jira 2025](https://softwarepricingguide.com/atlassian-jira-pricing-2025-every-plan-the-data-center-vs-cloud-cost-decision-and-the-price-increases-nobody-warned-you-about/) ·
[GreekReporter — Syrakis](https://greekreporter.com/2026/05/19/greek-engineer-laid-off-atlassian-reveals-infrastructure-software-giant/)

**Insider / sentiment (3–8/10; aggregate > any single item):**
[YouTube — "I was laid off by Atlassian"](https://www.youtube.com/watch?v=55pTFVoclvE) ·
[Threads/@carnage4life](https://www.threads.com/@carnage4life/post/DYe32q-FGIU/) ·
HN: [Jira is slow](https://news.ycombinator.com/item?id=25594451) ·
[over-engineered](https://news.ycombinator.com/item?id=23804620) ·
[Why Jira sucks](https://news.ycombinator.com/item?id=25590846) ·
[Slow death of Agile & Jira](https://news.ycombinator.com/item?id=41659128)

**Competitors / OSS (7–9/10):**
[Plane GitHub](https://github.com/makeplane/plane) · [Plane editions](https://developers.plane.so/self-hosting/editions-and-versions) ·
[OpenProject + XWiki](https://www.openproject.org/blog/open-source-jira-confluence-alternative/) ·
[BookStack](https://www.bookstackapp.com/about/open-source-documentation-software/) ·
[Outline (BSL)](https://github.com/outline/outline) ·
[Notion MCP server](https://github.com/makenotion/notion-mcp-server) · [Notion MCP docs](https://www.notion.com/help/notion-mcp) ·
[Linear ↔ agents](https://linear.app/integrations/notion-agent) ·
[Huly](https://openalternative.co/huly)

---

*Prepared as a research deliverable. Per the brief, no product code was modified.
Awaiting approval before implementing any slice in §11.*
