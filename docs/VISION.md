# Athena — Vision

> The north star. `AGENTS.md` says *how* we build; this says *what we are building
> toward* and *who for*, so every change can be measured against it. When a proposed
> feature doesn't serve the picture below, cut it or reshape it.

## One line

**Mission control for a one-person agent fleet.**

Athena is a self-hosted workspace where a single operator directs many AI agents.
Aegis (issues) and Mentor (docs) are the substrate; the real product is the *loop* by
which one human safely **delegates** work to agents, **watches** them, **steps in**
when needed, and **trusts** the result — because everything an agent does is
attributable and reversible.

## Who is at the helm

The **solo operator** (or a 2–5 person team), running agents *alongside* themselves.
They are a **conductor, not a doer**: their scarce resource is attention, and their job
is steering a fleet, not doing every task by hand. Every decision optimizes for that
person supervising many agents — **not** for a room of humans collaborating. That single
reframe is what separates Athena from Jira/Notion/Linear, which are built for teams of
people.

## The operator's loop (the spine of the roadmap)

Each phase names the target capability we invest in. This is the destination, not
a shipped-feature inventory; [`ARCHITECTURE.md`](ARCHITECTURE.md) records current
delivery. In particular, budgets, approval gates, agent pause/kill controls, and
general undo are roadmap goals rather than guarantees Athena makes today.

1. **Direct** — capture intent as work agents can pick up (issues with clear acceptance
   criteria, docs as playbooks).
2. **Delegate** — hand a task to a specific agent with a scoped token, a budget, and a
   rate limit; the automation engine routes work to agents on events, and each agent
   can pull a bounded, self-only inbox of its current assignments.
3. **Observe** — a live cockpit: what is each agent doing *right now*, run timelines,
   failures, token/rate/budget usage. The operator watches the fleet, not each task.
4. **Intervene** — steer by exception: approve/reject risky actions (human-in-the-loop),
   dry-run, pause or kill a misbehaving agent — without babysitting the rest.
5. **Trust / Learn** — the append-only activity log + run replay/lineage prove exactly
   what happened and let the operator **undo** it; corrections feed back into Mentor as
   durable context the agents read next time.

## Steering rules (every change is measured against these)

1. **API/MCP-first; the web UI is the operator's cockpit.** No capability ships without
   an MCP tool + REST endpoint. The web page exists to *supervise* the capability, never
   to be the only way in. (Extends the cardinal rule: web is a thin client; the API is
   agent-first.)
2. **Every agent action is attributable, reversible, and bounded.** Actor
   attribution exists today; optional run IDs and lineage enrich it. Scope, rate,
   and idempotency provide some bounding. Budgets, general reversibility, and
   approval are explicit roadmap requirements. Trust comes from *undo + inspect*,
   not from watching.
3. **The human steers by exception.** Default to letting agents run; surface *decisions*
   — failures, approvals, budget breaches — not noise.
4. **One operator, zero ops.** No feature may require a second human, a DBA, or a
   cluster. One process, one SQLite file, one-command deploy stays sacred.
5. **Stay lean — leanness is the moat.** Say no to multi-tenant SaaS, human-heavy
   permission schemes, workflow engines, and real-time multi-human collaboration.

## Non-goals (say no on purpose)

Multi-tenant SaaS · enterprise RBAC / permission schemes · a workflow/BPMN engine ·
real-time multi-human co-editing · JQL/custom-field kitchen sinks · a block editor ·
anything that assumes a team of humans rather than one human + N agents.

## How to use this doc

Before merging, ask: **does this help the solo operator conduct their agent fleet, and
does it pass the five steering rules?** If not, it doesn't belong here — no matter how
good the feature is in the abstract. Ship the smallest slice that advances one phase of
the loop, fully verified, as its own PR.
