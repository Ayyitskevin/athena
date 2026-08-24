# Docs index — one line per document

Forty-odd documents is too many to skim and too few to search well. This page
is the map: one honest line each, grouped by the question you arrived with.
When a one-liner and the document disagree, the document wins — fix the line.

## Start here

| Doc | One line |
|---|---|
| [VISION.md](VISION.md) | The north star — mission control for a one-person agent fleet — and the five steering rules every change is measured against. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The design of record: what we're building and why, for anyone (human or agent) joining later. |
| [../AGENTS.md](../AGENTS.md) | How we build in this repo: the cardinal rule, module lanes, the gate, branch/PR contract. Read before writing. |
| [QUICKSTART.md](QUICKSTART.md) | Two honest install paths: a disposable demo, or the instance you keep (bootstrap admin, onboard your agent). |
| [OPERATIONS.md](OPERATIONS.md) | The operator runbook: deployment gate, env vars, health, backups, API token minting/scopes/revocation. |

## Working as an agent (the product surfaces)

| Doc | One line |
|---|---|
| [AGENT_BOOT.md](AGENT_BOOT.md) | The canonical seat-boot block for an agent's rules file, the wiring check, and the sqlite escape-hatch policy. |
| [AGENT_API.md](AGENT_API.md) | **Generated** map of every MCP tool to its required scope and the REST call(s) behind it — regenerate, never edit. |
| [DESK.md](DESK.md) | `GET /desk` / `my_desk()` — who you are, what is asked of you, what you hold, what changed: one bounded read. |
| [OFFICE.md](OFFICE.md) | The cubicle inside the desk: one chair per agent, fenced paths, checkout hint. |
| [WORK_CONTEXT.md](WORK_CONTEXT.md) | The bounded read packet for one issue: the issue, neighbours, blockers, runbook — prefer it over five separate reads. |
| [QUERY.md](QUERY.md) | The work query language (GitHub-shaped, not Jira) — same syntax in the search box, REST, and saved filters. |
| [RUNS.md](RUNS.md) | Runs are a projection of the append-only activity log: begin, tag writes, heartbeat, replay, lineage. |
| [RUN_CONTROLS.md](RUN_CONTROLS.md) | Steering a live run by recorded request — between "let it run" and the blunt levers. |
| [RUN_LEARNINGS.md](RUN_LEARNINGS.md) | Closing the Trust/Learn loop: corrections feed back into Mentor as durable context agents read next time. |
| [WORKERS.md](WORKERS.md) | The worker registry and the cooperative kill: proving a *process* exists, beyond user + token + run. |
| [AGENT_BUDGETS.md](AGENT_BUDGETS.md) | Durable per-agent budgets — the "bounded" leg of attributable/reversible/bounded. |
| [APPROVALS.md](APPROVALS.md) | Human-in-the-loop gates on risky action kinds — the operator's Intervene step. |
| [ANSWERABILITY.md](ANSWERABILITY.md) | Asks and answers per agent, deliberately never a score. |
| [ACTIVE_WORK.md](ACTIVE_WORK.md) | Which agents hold which leases right now, with the evidence behind each claim. |
| [DISPATCH.md](DISPATCH.md) | Handing work to an external executor (Icarus): control plane here, execution fleet there, no shared store. |
| [FLEET_METRICS.md](FLEET_METRICS.md) | Fleet throughput metrics — the first accepted vertical slice. |
| [RUNTIME_RECIPE.md](RUNTIME_RECIPE.md) | Wiring a real agent runtime (MCP config, token) to close the operator loop — Athena ships no runtime on purpose. |
| [../examples/desk_loop.md](../examples/desk_loop.md) | A prompt you can hand an agent verbatim: desk → claim one thing → work → learn → put it down. |

## The knowledge module (Mentor)

| Doc | One line |
|---|---|
| [EDITING.md](EDITING.md) | Editing and leaving: drafts, versions, and never losing a keystroke. |
| [EMBEDS.md](EMBEDS.md) | Live embeds — a fenced block on a page renders real issues at view time. |
| [GRAPH.md](GRAPH.md) | The link graph behind `[[wikilinks]]` and `[[ATH-12]]` refs, and what reads it. |
| [PLAYBOOKS.md](PLAYBOOKS.md) | Docs that *start* work: a page that files the issues it describes. |
| [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md) | Watches and notifications: subscribing your inbox to a target. |
| [PLANNING.md](PLANNING.md) | The timeline and live rollups drawing sprints and typed dependencies together. |

## Trust, audit, and safety

| Doc | One line |
|---|---|
| [TRAIL_INTEGRITY.md](TRAIL_INTEGRITY.md) | The hash chain over the activity log — tamper-evidence for the append-only trail. |
| [UNDO.md](UNDO.md) | Undo by compensation: reversing agent writes without mutating an append-only history. |
| [EXCEPTION_SURFACES.md](EXCEPTION_SURFACES.md) | The attention rollup and security signals — surfacing decisions, not noise. |
| [WORKFLOW_GATES.md](WORKFLOW_GATES.md) | The opt-in blocked-issue close policy (off by default). |
| [AUTOMATION_SCHEDULES.md](AUTOMATION_SCHEDULES.md) | Scheduled automation rules: trigger types, occurrence state, firing history. |

## Building and releasing Athena itself

| Doc | One line |
|---|---|
| [AI_DEVELOPMENT.md](AI_DEVELOPMENT.md) | How this repo is itself developed with AI agents, and what that provenance looks like. |
| [COMMAND_MIGRATION.md](COMMAND_MIGRATION.md) | The one-command-owns-each-write migration: target shape, debtor list, how to pay debt down. |
| [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) | "Phosphor Ink" — the visual and IA system; read before touching `styles.css` or a template. |
| [TOKENS.md](TOKENS.md) | **CSS design tokens** (every value in `styles.css` §1) — *not* API tokens; those are in QUICKSTART.md and OPERATIONS.md. |
| [DOCKER.md](DOCKER.md) | Running Athena in a container under the same fail-closed deployment gate. |
| [FORGE.md](FORGE.md) | Commits, branches, and PRs visible from the work item that caused them. |
| [RELEASE_READINESS.md](RELEASE_READINESS.md) | The evidence behind `0.1.0a1` — a checklist, not a production claim. |
| [ROADMAP.md](ROADMAP.md) | Where this is going: tracker + wiki + notebook, minus the reasons people hate each. |
| [PRUNE_LEDGER.md](PRUNE_LEDGER.md) | What we cut and why — VISION.md's "cut or reshape" rule, made visible. |
| [DECISIONS_PENDING.md](DECISIONS_PENDING.md) | Items blocked on an operator decision, not on engineering. |

## Subdirectories

| Path | One line |
|---|---|
| [plans/](plans/) | Long-form working guides for major pushes (expansion, remediation, performance/adoption) and their reports. |
| [research/](research/) | Roadmap research notes feeding future work. |
| [assets/](assets/) | Images used by the docs (the operator-loop diagram). |

## Looking for something that isn't where you expected?

- **API bearer tokens** (minting, scopes, revocation): [QUICKSTART.md](QUICKSTART.md) path B and [OPERATIONS.md](OPERATIONS.md) — not [TOKENS.md](TOKENS.md), which is CSS.
- **The REST routes**: the app's own `/openapi.json` (browse at `/redoc`) is
  the full reference, and [AGENT_API.md](AGENT_API.md) maps MCP tools to
  scopes and routes; `/aegis/*` browser paths are session+CSRF web routes,
  *not* the API an agent token can call.
- **How to contribute code**: [../AGENTS.md](../AGENTS.md), not this folder.
