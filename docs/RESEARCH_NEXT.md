# Athena — What to Build Next & How to Differentiate

> **Provenance & integrity.** This report is the output of Athena's `deep-research`
> harness — fan-out web search → source fetch/extract → **3-vote adversarial
> verification** (a claim needs 2 of 3 refutations to be killed) → synthesis. Run
> stats: 5 search angles, 25 sources fetched, 33 claims extracted, **22 of 25
> confirmed**, 3 killed, and one Deloitte forecast deliberately excluded for failing
> verification. The two most load-bearing citations were independently re-confirmed
> outside the harness: the Atlassian reorganization/layoffs (corroborated by CNBC,
> TechRepublic, The Next Web, and Atlassian's own newsroom) and arXiv 2605.21997
> *"The Log is the Agent"* (May 2026). Treat citations as research **leads**, not
> settled fact; figures are current as of mid-2026 and should be re-checked before
> any major positioning decision. Companion to
> [`RESEARCH_ROADMAP.md`](RESEARCH_ROADMAP.md) (the original Phase-1 research).


**Executive summary.** Athena already has the hard-to-copy substance: per-project statuses, hierarchy, typed links, a kanban board, FTS5 search, docs with versions, an event feed with webhooks, notifications, scoped tokens, and an append-only activity log. The single largest remaining opening is to lean *harder* into the one thing incumbents structurally cannot match — being **agent-native and self-hosted from the foundation up** — rather than chasing Jira/Confluence feature depth. The market is moving in Athena's direction: the dominant incumbent is reorganizing the whole company around an integrated AI-driven platform and has shifted its center of gravity decisively to cloud ([theregister.com](https://www.theregister.com/2026/03/11/atlassian_layoffs/)), while the leading self-hosted OSS rivals are either operationally heavy to deploy ([github.com](https://github.com/makeplane/plane/issues/8708), [news.ycombinator.com](https://news.ycombinator.com/item?id=41833902)) or paywall core capabilities like SSO behind a commercial edition, driving real user churn ([github.com](https://github.com/orgs/makeplane/discussions/1266)). The highest-leverage next builds are the ones that turn Athena's existing append-only log into a *replayable, forkable, lineage-bearing* substrate and make agents true delegatable teammates — capabilities validated by current event-sourced-agent research ([arxiv.org](https://arxiv.org/abs/2605.21997)) and by Linear's first-mover agent model ([eesel.ai](https://www.eesel.ai/blog/linear-ai)). The differentiation thesis, in one line: a genuinely full-featured, single-file self-hosted tool where the audit log *is* the source of truth and agents are first-class, delegatable, fully-audited actors — the inverse of AI bolted onto a 20-year-old product.

---

## Market signals (2025–2026)

**Fact — The incumbent is reorganizing the entire company around an integrated "system of work" platform, not its legacy point tools.** Atlassian's CEO stated the company is "changing the way we work and reorganising around our System of Work to move faster" ([theregister.com](https://www.theregister.com/2026/03/11/atlassian_layoffs/)).

**Fact — The incumbent's strategic and financial center of gravity has shifted decisively to cloud, away from self-hosted/Data Center.** It reported over 25% cloud revenue growth and 40%+ growth in remaining performance obligations, plus securing 600 customers spending over $1M/year ([theregister.com](https://www.theregister.com/2026/03/11/atlassian_layoffs/)). (Verifier note: primary filings confirm Data Center is under a formal end-of-life timeline, reinforcing the cloud-first shift.)

**Fact — The incumbent cut roughly 10% of its workforce (~1,600 employees), framed as self-funding AI and enterprise-sales investment rather than direct AI replacement.** The CEO said the cuts were "to self-fund further investment in AI and enterprise sales, while strengthening our financial profile" ([theregister.com](https://www.theregister.com/2026/03/11/atlassian_layoffs/)).

**Fact — The incumbent's AI bet is concentrated in a new "Rovo" AI suite, which it claims has surpassed five million users.** This makes AI assistants the incumbent's headline differentiator ([theregister.com](https://www.theregister.com/2026/03/11/atlassian_layoffs/)).

**Fact — Analysts forecast a structural shift in how software is bought and used.** Deloitte's 2026 TMT Predictions argue SaaS will evolve "towards a federation of real-time workflow services" that "create, integrate, and orchestrate AI agents," and that "how organizations purchase and use software could shift dramatically" ([deloitte.com](https://www.deloitte.com/us/en/insights/industry/technology/technology-media-and-telecom-predictions/2026/saas-ai-agents.html)).

**Fact — Seat-based licensing is forecast to give way to usage/outcome-based pricing.** Deloitte cites Gartner's projection that "by 2030, at least 40% of enterprise SaaS spend will shift toward usage-, agent-, or outcome-based pricing" ([deloitte.com](https://www.deloitte.com/us/en/insights/industry/technology/technology-media-and-telecom-predictions/2026/saas-ai-agents.html)).

**Fact — Agentic-AI investment is forecast to reach high adoption in 2026.** Deloitte predicts "up to half of organizations will put more than 50% of their digital transformation budgets toward AI automation in 2026," with "perhaps reaching 75%" of companies investing in agentic AI ([deloitte.com](https://www.deloitte.com/us/en/insights/industry/technology/technology-media-and-telecom-predictions/2026/saas-ai-agents.html)).

### Where the OSS self-hosted alternatives fall short

**Fact — Plane is operationally heavy and painful to self-host.** A self-hosting user reported: "Anyone trying to self-host Plane should be prepared for a lot of painful debugging. I never saw such a badly designed application - 7+ containers, no clear boundaries, almost 200 lines of ENV variables" ([github.com](https://github.com/makeplane/plane/issues/8708)). (Verifier note: the official compose file defines ~13 services, so "7+ containers" is conservative.)

**Fact — Plane's self-hosted onboarding has a silent first-step failure.** Its first onboarding button "tries to redirect to `https://domain.example.com/god-mode` (no trailing / ) and this just stuck forever without any warning," causing user drop-off ([github.com](https://github.com/makeplane/plane/issues/8708)). (Verifier note: a maintainer PR fixing the identical trailing-slash god-mode redirect was merged in 2024, and the symptom recurred in 2026 — a persistent friction point, not a one-off.)

**Fact — Plane has release/distribution-channel discipline problems in its self-hosting pipeline.** A Plane organization member confirmed: "2.3.7 is not an official release. We made those changes exclusively for a specific customer. Ideally, the version should not be available if you are using prime-cli to install it" — yet it had reached an installer and broke a self-hoster's file permissions ([github.com](https://github.com/makeplane/plane/issues/8708)).

**Fact — Plane paywalls auth/SSO and project-planning features behind its paid edition.** A user reported "OIDC login and gantt connections are only in payed version. OIDC is only a login" ([github.com](https://github.com/orgs/makeplane/discussions/1266)). (Verifier note: corroborated by Plane's official editions docs — the Commercial Edition "adds SSO," and OIDC was moved behind a paywall in v2 at ~$8/seat per issue #8047.)

**Fact — Users perceive self-hosted Plane as a funnel toward the paid product, not a fully-functional open tool.** A user described the self-hosted experience: "This can sometimes feel like a free trial with extra steps" ([github.com](https://github.com/orgs/makeplane/discussions/1266)).

**Fact — Paywalling core features drove at least one user to abandon Plane.** "The decision to move essential features behind a paywall lead me to drop plane" ([github.com](https://github.com/orgs/makeplane/discussions/1266)). Others in the same thread reported switching to YouTrack, OpenProject, and Huly over the same grievance.

**Fact — Huly is operationally heavy to self-host.** Deploying it "requires running 5 different open source servers (databases, proxies, etc), and 5 different services that form part of this suite. If self-hosting this in a company, you need to be an expert in lots of different systems and potentially how to scale them, back them up, etc" ([news.ycombinator.com](https://news.ycombinator.com/item?id=41833902)). (Verifier note: the official compose file actually defines ~9 Huly application services plus 5 infrastructure servers — heavier than the quote.)

**Interpretation.** The OSS field has a clear, repeating shape: deployment complexity (Plane, Huly), distribution/release fragility (Plane), and open-core paywalls on the exact capabilities self-hosters most want — SSO and planning (Plane). The two failure modes — "too hard to run" and "the free version is a trial" — are precisely the seams Athena's single-file, no-build-chain, no-enterprise-gate posture is built to exploit.

---

## Where Athena can differentiate

The thesis: a self-hosted, agent-native tool can differentiate where incumbents *structurally cannot* — by treating agents as first-class actors, by making the event log the source of truth (not just an audit trail), and by giving users full data ownership in a single file with no enterprise gate.

**Fact — An append-only event log can serve as the single source of truth, with working state as a deterministic projection of that log.** "The append-only event log is the source of truth; the working graph is a deterministic projection of that log; and behaviors ... react to changes in the graph and emit new events" ([arxiv.org](https://arxiv.org/abs/2605.21997)). This is also an established, production-proven pattern independent of the preprint (event-sourcing + CQRS).

**Fact — Making the log the source of truth yields three capabilities ordinary "memory" systems do not provide.** "This single design decision yields three properties that retrieval-and-summarization memory systems do not provide: deterministic replay of any run from its log, cheap forking that branches a run at any event without re-executing the shared prefix, and end-to-end lineage from a high-level goal down to the individual model call that produced each artifact" ([arxiv.org](https://arxiv.org/abs/2605.21997)).

**Fact — Coordination can happen entirely through the shared state projected from the log, with no component directly instructing another (a blackboard/event-driven model, not direct RPC).** "No component instructs another; coordination happens entirely through the shared graph" ([arxiv.org](https://arxiv.org/abs/2605.21997)).

**Fact — Sound replay is not free; it requires an explicit "determinism contract."** The research presents "a determinism contract that makes replay sound, and a worked diligence example whose full causal structure is reconstructable from the log alone" ([arxiv.org](https://arxiv.org/abs/2605.21997)).

**Interpretation.** Athena already has the rare half of this architecture — an append-only activity log that the docs explicitly treat as an event source, plus an event feed and outbound webhooks. The differentiation move is to make that log *load-bearing for agents*: deterministic replay of a run, forking a run from any event, and end-to-end lineage from a goal down to each action. None of these are things a cloud-first incumbent bolting AI onto a permissions-and-UI stack can easily retrofit, and none are offered by the heavier OSS rivals. The blackboard model maps directly onto Athena's existing event-feed-plus-webhooks plumbing: agents react to the shared state and emit events rather than calling each other. The determinism-contract finding is the honest caveat — replay/lineage guarantees require constraining how event handlers behave, so the value is real but not automatic.

**Fact — A first-mover competitor has already defined the agent-as-teammate primitives.** Linear treats agents as "app users" that "behave similar to other users in a workspace and can be @-mentioned, delegated issues through assignment, create and reply to comments, collaborate on projects and documents, etc" ([eesel.ai](https://www.eesel.ai/blog/linear-ai), verbatim from Linear's own docs).

**Fact — That competitor has a concrete human+agent delegation/permission model.** "When an issue gets delegated to an agent, the human user remains the primary assignee, while the agent is added as a contributor ... Team membership is set when the agent integration is added to a workspace and can be changed by an admin at any time" ([eesel.ai](https://www.eesel.ai/blog/linear-ai)).

**Fact — That competitor frames its agent platform as building/deploying full workspace-member teammates.** "Linear for Agents allows you to build and deploy AI agents that work alongside you as teammates, work on complex tasks together or delegate issues end-to-end, with agents being full members of your Linear workspace" ([eesel.ai](https://www.eesel.ai/blog/linear-ai)).

**Fact — That competitor positions itself as an AI-development "command center" integrating third-party coding agents.** "Linear is positioning itself to be a command center for AI-assisted development, built to work with third-party AI agents, like Cursor or Devin, which can be assigned technical tasks right from a Linear issue" ([eesel.ai](https://www.eesel.ai/blog/linear-ai)).

**Interpretation.** Linear has validated the *demand* and published a clean blueprint for agent-native primitives — but it is closed and SaaS-only, so it cannot be self-hosted and cannot offer data ownership or local replay. Athena already has the scaffolding Linear's model needs (scoped bearer tokens, an MCP server, an audit log). The specific gap to close is the **delegation/contributor model**: making an agent a first-class actor that can be delegated an issue while the human remains primary assignee and accountable, with admin-controlled team/scope access. That is the smallest concrete step that turns Athena's "agents can write via tokens" into "agents are teammates," and it is directly copyable from a proven design without copying any incumbent's IP.

---

## Migrating off Atlassian: the minimum viable set

Teams leaving the incumbent need a small, specific set of capabilities to feel safe. The market signals above sharpen which ones matter.

**Recommendation — Import paths (highest migration blocker).** Movers need to get data *in* cleanly (issues, pages, version history, links). The OSS churn evidence shows users actively shopping alternatives and citing "imported X, seems to just work perfectly fine" as a deciding factor ([github.com](https://github.com/orgs/makeplane/discussions/1266)). A reliable import is the gate to adoption.

**Recommendation — SSO/SAML/OIDC and SCIM, shipped free, not gated.** This is the single clearest competitive opening: a self-hosted OSS rival paywalls OIDC behind its commercial edition, and that paywall demonstrably drove churn ([github.com](https://github.com/orgs/makeplane/discussions/1266)). Shipping basic OIDC SSO in the free, self-hosted product directly attacks a competitor's most-resented limitation. (Scope it minimally — login + provisioning — not a full enterprise IAM suite.)

**Recommendation — Permissions parity at a coarse, comprehensible level.** Movers need confidence that confidential work stays confidential. The incumbent's strength here is layered, scheme-based permissions that are powerful but hard to reason about; Athena's opening is *coarse-but-clear* read authorization and per-space/project visibility, not a scheme system.

**Recommendation — Automation/rules and dashboards/reporting at a deliberately small scope.** Movers expect "when X then Y" automation and basic dashboards. Athena's event-feed/webhooks already provide the substrate; a lean rules layer that reacts to events (blackboard-style, [arxiv.org](https://arxiv.org/abs/2605.21997)) covers most real needs without importing a workflow engine.

**Interpretation.** The minimum viable set for movers is: clean import, free SSO/OIDC, coarse permissions parity, and lean event-driven automation. SCIM and rich dashboards are second-order. The decisive differentiator versus OSS rivals is *not gating any of this* behind a paid tier — the churn evidence shows paywalling these exact features is what loses users.

---

## What to build next (ranked)

Ranked by impact-vs-effort. This table is reconciled against current `main` as of
2026-06-30, so capabilities that have landed are marked as shipped V1 instead of
being treated as open backlog. All items are **Recommendations**.

| # | Capability | Status | Why it matters | Next slice |
|---|---|---|---|---|
| 1 | **Import path from common sources** (issues/pages + history + links) | Hardened V1 (2026-07): components/issuetype→labels, attachment_manifest, `athena-validate-source` preflight | The adoption gate for movers; users cite smooth import as a deciding factor ([github.com](https://github.com/orgs/makeplane/discussions/1266)). | Feed real operator dumps into validate-source; blob policy still deferred |
| 2 | **Run replay artifact** (portable replay manifest over one run) | Shipped V1 | Athena now freezes one run's ordered events plus lineage metadata for agent handoff and audit. | Deepen with signed artifacts or bundle import only after real operator demand |
| 3 | **Agent administration V2** | V2 REST + web revoke-all (2026-07): `/api/admin/agents`, mint/revoke tokens, memberships, role/flag | Delegation + contributor model exists; admin-controlled scope/team access is the next teammate primitive. | Optional default-scope policy and bulk project grants UI |
| 4 | **API safety for agent loops** | Partial V1 shipped (idempotency + per-token/anon rate limits) | Idempotency exists; agents still need efficient bulk actions and update-race protection. | Bulk actions / If-Match next |
| 5 | **One-command / few-container self-host packaging hardening** | Hardened V1 (2026-07): `deploy/` systemd+env, `athena-backup-prune` | `athena-doctor` improves deploy preflight; packaging/retention can turn single-file SQLite into a self-hosting moat. | Off-host rsync helper; flow production cutover when ready |
| 6 | **Deterministic run replay + lineage over the activity log** | Shipped V1 | `/events`, run lineage, replay-safe fields, and determinism docs now exist. | Deepen via replay artifact above |
| 7 | **Run forking** | Shipped V1 | Fork contract endpoint, headers, MCP parity, and web copy blocks now exist. | Use it from replay/import workflows |
| 8 | **Agent-as-teammate: delegation + contributor model** | Shipped V1 | Issues can be delegated to agent users while preserving assignee accountability. | Deepen via agent admin above |
| 9 | **Free OIDC SSO (self-hosted, no enterprise gate)** | OIDC shipped; SAML/SCIM deferred | Basic OIDC closes the near-term self-host SSO gap. | Revisit SAML/SCIM only on concrete demand |
| 10 | **Lean event-driven automation/rules** | Shipped V1 | Event feed, webhooks, and automation rules exist without a workflow-engine trap. | Keep rules lean; avoid Jira-style schemes |
| 11 | **Coarse permissions parity** | Shipped V1 | Project/space visibility and membership gate read surfaces. | Decide on optional global read-auth mode before public exposure |
| 12 | **Basic dashboards/reporting** | Open | Movers expect at-a-glance status; cheap as SQL over data Athena already owns. | Counts/rollups only after portability/replay work |

**Sequencing rationale.** The differentiation core has V1 coverage now: scoped
tokens, MCP, event feed, webhooks, delegation, lineage, forking, manifest-gated
Athena-to-Athena portability, run replay artifacts, and basic Jira/Confluence JSON
bundle mappers. The next best work is to harden migration against real-world
exports and make agent operation safer: real Atlassian sample fixtures, then
agent-admin/API-safety/packaging follow-ups.

---

## What NOT to build

Scope traps for a small self-hosted tool, with reasoning.

**Recommendation — Do not build a heavyweight workflow engine** (transition conditions/validators/post-functions, screen schemes). This is the incumbent's deepest moat *and* the single most-cited source of its complexity. A small tool cannot win on configurability and should not try; the event-driven automation in item 6 covers the real need.

**Recommendation — Do not build scheme-based, multi-layered permissions.** The incumbent's layered permission schemes are powerful but notoriously hard to reason about. Athena's edge is coarse, comprehensible visibility — adding a scheme system would import the complexity without the moat.

**Recommendation — Do not adopt a heavy multi-service deployment architecture.** The evidence is direct: Plane's 7+ containers and ~200 env lines are a documented source of self-host pain ([github.com](https://github.com/makeplane/plane/issues/8708)), and Huly's ~10-service stack demands cross-system expertise ([news.ycombinator.com](https://news.ycombinator.com/item?id=41833902)). Athena's single-file SQLite, no-JS-build-chain posture is a differentiator to defend, not dilute.

**Recommendation — Do not build an open-core paywall on core capabilities (SSO, planning, audit).** Paywalling these is exactly what drove documented churn away from a rival ([github.com](https://github.com/orgs/makeplane/discussions/1266)). For a self-hosted tool, the full-featured free product *is* the differentiation.

**Recommendation — Do not chase the incumbent's AI-assistant surface as a feature race.** The incumbent's Rovo suite claims 5M+ users and a large org reorganized around it ([theregister.com](https://www.theregister.com/2026/03/11/atlassian_layoffs/)); Athena cannot and should not out-feature a company self-funding AI with 1,600 layoffs. The structural play (log-as-truth, replay, lineage, delegatable agents) is defensible; a chatbot race is not.

**Recommendation — Do not ship customer-specific or unofficial builds into public install channels.** Plane's leaked `2.3.7` build is a cautionary tale: a member confirmed it "is not an official release ... should not be available," yet it broke a self-hoster ([github.com](https://github.com/makeplane/plane/issues/8708)). Release discipline is cheap differentiation for a self-hosted tool.

---

## Caveats & open questions

**Source-strength caveats.**
- The strongest differentiation evidence (deterministic replay, forking, lineage, blackboard coordination, determinism contract) comes from a single recent arXiv preprint ([arxiv.org](https://arxiv.org/abs/2605.21997)) — non-peer-reviewed, single-author, with one worked example and no scale/benchmark data. It is corroborated at the *pattern* level by the established event-sourcing/CQRS literature, but the agent-runtime specifics are an extrapolation to a work-management tool. Treat these as a validated architectural bet, not a proven at-scale result.
- The competitor agent-model claims come from a secondary source ([eesel.ai](https://www.eesel.ai/blog/linear-ai)) whose wording is verbatim-identical to the vendor's own docs, so the facts are reliable; the "command center" framing is the vendor's positioning, not independent assessment.
- The OSS-shortfall evidence is primary (GitHub issues, a maintainer statement, an HN comment, [github.com](https://github.com/makeplane/plane/issues/8708), [github.com](https://github.com/orgs/makeplane/discussions/1266), [news.ycombinator.com](https://news.ycombinator.com/item?id=41833902)) but is partly user sentiment and single-incident anecdote — directionally sound, not statistically representative. The "Gantt connections are paid" and "OIDC login-only" sub-details rest on single forum quotes and are weaker than the core SSO-paywall point.
- The Deloitte forecasts ([deloitte.com](https://www.deloitte.com/us/en/insights/industry/technology/technology-media-and-telecom-predictions/2026/saas-ai-agents.html)) are inherently speculative forward-looking predictions; they are accurately attributed but should be read as directional, not deterministic. (Note: a related Deloitte "35% of point-product SaaS will be replaced by agents by 2030" claim failed verification and is deliberately excluded.)

**Open questions.**
- **Determinism scope.** Deterministic replay requires a determinism contract on event handlers ([arxiv.org](https://arxiv.org/abs/2605.21997)); LLM calls are non-deterministic by default. How much of Athena's agent activity can realistically be made replayable, and at what handler-design cost?
- **Migration priority.** Which import source matters most to actual movers (issue tracker vs. wiki vs. CSV)? The evidence shows import is a deciding factor but not which format leads.
- **Permissions depth.** How coarse can read authorization stay before org adopters demand more? The minimum viable set assumes coarse visibility suffices; that is an assumption, not a measured fact.
- **Time-sensitivity.** The incumbent's reorganization, cloud shift, and Data Center end-of-life are actively unfolding through 2026 ([theregister.com](https://www.theregister.com/2026/03/11/atlassian_layoffs/)); the agent-native competitor landscape (Linear, third-party coding agents) is moving monthly. The market-signal facts here are current as of mid-2026 and should be re-checked before any major positioning decision.
