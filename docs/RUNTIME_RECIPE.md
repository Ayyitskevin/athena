# Runtime recipe — close the operator loop with a real agent

Athena ships no agent runtime, on purpose: it is the workspace agents act in, not
another framework to adopt. That leaves one gap this document closes. You can
install Athena, read the vision, and still not have watched an agent finish a
piece of delegated work — which is the only thing that makes the rest of it real.

**By the end of this page an agent will have claimed a delegated issue, worked
it, recorded what it learned, released the claim, and answered a steer you sent
mid-flight — and every one of those steps will be a row on the trail you can
replay.** It takes about ten minutes and needs nothing but Athena, Claude Code,
and a terminal.

This is the *operator* side. The agent-side vocabulary — what the tools mean and
how the trail thinks — lives in the in-app Field Guide (`athena-field-guide`).
The copy-paste prompt is [`examples/desk_loop.md`](../examples/desk_loop.md).

---

## 0. What you need

- Athena installed and running (see [QUICKSTART.md](QUICKSTART.md), or just run
  `athena-demo` and use the workspace it prints).
- The `mcp` extra: `pip install -e ".[dev,mcp]"`.
- [Claude Code](https://claude.com/claude-code), or any MCP client. The recipe
  uses Claude Code because the config below is copy-paste for it; nothing in the
  loop is Claude-specific.

If you are using `athena-demo`, it already printed everything in step 1 and 2 —
skip to step 3 and use the config it gave you.

---

## 1. An agent account and a scoped token

An agent is a **user** in Athena, not an API key with a nickname. It gets its own
identity, its own row in every audit event, and its own least-privilege token.

```bash
# As an admin, over HTTP (or use the web UI: Admin → Agents → Onboard)
curl -sS -X POST http://127.0.0.1:8000/users/onboard_agent \
  -H 'Content-Type: application/json' \
  -H 'X-Athena-Actor: 1' \
  -d '{
        "email": "sol@athena.local",
        "name": "Sol Builder",
        "token_name": "desk-loop",
        "scopes": ["read", "issue:write", "docs:write"]
      }'
```

The response carries `user`, `token` (the raw secret, shown **once**), and
`mcp_config` — a ready-to-paste MCP client config already wired to this
deployment and this token, so step 3 is a copy rather than an assembly job.
Athena stores only the token's hash, and the mint itself is already on the
activity trail: an agent's credential coming into existence is an auditable
event, not a side effect.

Give it the narrowest scopes the loop needs. `read`, `issue:write`, and
`docs:write` are enough for everything below; the agent cannot onboard other
agents, set budgets, or decide approvals with these, and that is the point.

## 2. Something to delegate

An agent works what it is *given*. Create an issue and delegate it:

```bash
curl -sS -X POST http://127.0.0.1:8000/issues \
  -H 'Content-Type: application/json' -H 'X-Athena-Actor: 1' \
  -d '{"title": "Tidy the release checklist", "body": "Trim the stale steps.",
       "project_id": 1, "priority": "medium"}'

# then delegate it to the agent (Admin → Issue → Delegate, or the API)
curl -sS -X POST http://127.0.0.1:8000/issues/1/delegate \
  -H 'Content-Type: application/json' -H 'X-Athena-Actor: 1' \
  -d '{"user_id": 2}'
```

Delegation is what makes the issue appear on the agent's desk. An agent that was
never delegated anything has an empty desk, which is the correct answer.

## 3. Point Claude Code at Athena

Athena hands you this config already filled in — it is the `mcp_config` field of
the onboarding response above, and `athena-demo` prints the same thing. Drop it
in your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "athena": {
      "command": "athena-mcp",
      "env": {
        "ATHENA_BASE_URL": "http://127.0.0.1:8000",
        "ATHENA_TOKEN": "ath_your_token_here"
      }
    }
  }
}
```

The MCP server shows the agent **only the tools its token's scopes allow**. A
read-only token sees no write tools at all — the tool list is itself a boundary,
not just an error you hit later.

## 4. Run the loop

Paste [`examples/desk_loop.md`](../examples/desk_loop.md) into Claude Code as the
prompt. It is written to be handed to an agent verbatim.

Watch it in the Athena UI while it runs. You should see, in order:

| The agent does | You see |
|---|---|
| `my_desk()` | nothing yet — a read |
| `claim_issue(...)` | the issue shows a live **claim** with an expiry |
| `heartbeat_agent_run(...)` | a run **check-in**, visible on Mission Control |
| `update_issue` / `comment_on_issue` | activity rows attributed to the agent |
| `record_run_learning(...)` | a learning promoted into the issue's runbook |
| `complete_claim(...)` | the claim released, the work still attributed |

## 5. Interrupt it — this is the part worth watching

While the agent is working, send a steer from **Admin → Run controls** (or the
run's lineage page):

> Narrow the scope: just the stale steps, leave the wording alone.

Then watch what Athena does and does not do. It **records the ask**. It does not
signal the agent's process, pause it, or inject anything into its context —
Athena has no channel to do that and does not pretend otherwise. The agent sees
the control the next time it looks (`my_desk()` or `list_run_controls()`), and
answers with `acknowledge_run_control` and then `complete_run_control`.

If it never looks, the control **expires**. It reads as expired, never as obeyed.
That distinction is the honest core of the whole feature: an operator who cannot
tell "the agent complied" from "the agent never noticed" does not have
supervision, they have a dashboard.

## 6. Approve the close

If you gated the agent (`set_approval_policy` on `issue.close`, which
`athena-demo` does for you), its attempt to close the issue is **refused** and
recorded as a pending ask instead. Answer it from **Admin → Agents**.

The trail now holds the ask, your decision, and the close as three separate
facts, each with its own actor and timestamp. Nobody has to remember who let it
land.

---

## What you just proved

- An agent did real work under its own identity, with its own least-privilege
  credential, and every action is attributable.
- You bounded it (scopes, budget, approval gate) and interrupted it (run
  control), and both are recorded honestly — including the case where the agent
  did not answer.
- The whole thing ran on one machine, one process, one SQLite file, with no
  vendor between you and your data.

## What this does not do

Athena does not run your agent. There is no scheduler, no retry loop, no
supervision tree — if your agent crashes, Athena knows only that the claim's
lease expired and the check-ins stopped, which is exactly what it can honestly
say. Athena is where the work, the authority, and the evidence live; the runtime
is yours.

## Where to go next

- [`docs/TOKENS.md`](TOKENS.md) — scopes, minting, revocation
- [`docs/DESK.md`](DESK.md) — what `my_desk()` returns and why it is one read
- [`docs/RUNS.md`](RUNS.md) and [`docs/RUN_LEARNINGS.md`](RUN_LEARNINGS.md) — run
  identity, check-ins, replay, and how a learning becomes a runbook
- [`docs/APPROVALS.md`](APPROVALS.md) — the human-in-the-loop gate in step 6
- [`docs/AGENT_BUDGETS.md`](AGENT_BUDGETS.md) — ceilings and what happens at zero
- [`docs/VISION.md`](VISION.md) — why the loop is shaped this way
- The in-app **Field Guide** (`athena-field-guide <db>`) — the agent-side manual
