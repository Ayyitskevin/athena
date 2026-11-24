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

The requests in steps 1 and 2 carry an administrator's bearer token. Sign in as
the administrator — the login `athena-demo` printed, or the one you bootstrapped
in [QUICKSTART.md](QUICKSTART.md) — open **Delegate → Tokens**
(`/settings/tokens`), create a token with the **Admin** scope, and export it as
`ATHENA_ADMIN_TOKEN`. It is shown once; revoke it on the same page when you are
done.

```bash
# As an admin, over HTTP (or use the web UI: Admin → Agents → Onboard)
curl -sS -X POST http://127.0.0.1:8000/users/onboard_agent \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ATHENA_ADMIN_TOKEN" \
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
  -H 'Content-Type: application/json' -H "Authorization: Bearer $ATHENA_ADMIN_TOKEN" \
  -d '{"title": "Tidy the release checklist", "body": "Trim the stale steps.",
       "priority": "medium"}'

# then delegate it to the agent (Admin → Issue → Delegate, or the API)
curl -sS -X POST http://127.0.0.1:8000/issues/1/delegate \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $ATHENA_ADMIN_TOKEN" \
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
      "command": "/home/you/athena/.venv/bin/athena-mcp",
      "env": {
        "ATHENA_BASE_URL": "http://127.0.0.1:8000",
        "ATHENA_TOKEN": "ath_your_token_here",
        "ATHENA_MCP_PROFILE": "seat+knowledge"
      }
    }
  }
}
```

The MCP server uses the token's scopes to hide tools it cannot call; the REST
API still enforces authorization. If Athena rejects the token at startup (401,
or 403 for a paused or removed account), `athena-mcp` exits with a message
naming the cause instead of starting. If Athena cannot be reached or answers
with a server error, the server warns and presents the full tool surface, while
REST refusals remain in force.

`command` is the absolute path of the `athena-mcp` script installed beside the
Python that runs Athena, so the client needs no activated venv; the path above
is a placeholder for yours. `ATHENA_MCP_PROFILE` is chosen from the token's
scopes: `seat+knowledge` for a token with `docs:write`, as here (37 tools for
this token class, including `record_run_learning`), and `seat` otherwise (26
tools for issue work). Onboarding's optional `profile` prints another one. Profiles intersect scopes and never add permission. An
unset profile still means `full`, and an unknown profile refuses startup. See
[AGENT_BOOT.md](AGENT_BOOT.md) for the exact sets. Both issue reads accept
`issue_id=...` or `ref="ATH-12"`; supply exactly one. After reading the full
work context, `brief=true` keeps the issue, issue ETag, dependencies,
warnings and work-order report for a compact re-read.

For a conductor coordinating architect, builder and reviewer accounts, use the
39-tool `conductor` profile (onboard it with `"profile": "conductor"`) and
[CONDUCTOR_RECIPE.md](CONDUCTOR_RECIPE.md).
It adds assignment, delegation, dependencies and visible project observation to
`seat`; it does not add admin authority or launch workers. Keep this recipe's
`seat+knowledge` profile when the task needs pages or `record_run_learning`.
Use `get_project_brief(project_id=..., limit=5)` for a compact visible snapshot of
claims, blockers, awaiting-resume handoffs and historical completion reports.
Its per-section limit is 1..20; clipping and advisory reports remain explicit.
It neither advances a cursor nor supplies a claim precondition.

## 4. Run the loop

Paste [`examples/desk_loop.md`](../examples/desk_loop.md) into Claude Code as the
prompt. It is written to be handed to an agent verbatim.

For server-selected work, call
`claim_next_ready(limit=1, project_id=..., idempotency_key=...)` (REST:
`POST /issues/claim-next`). Omit `project_id` for all eligible projects plus backlog;
`limit` is 1–5 candidates, and one call acquires at most one lease. Selection and
acquisition are atomic, and any active possession by the account returns
`already_holding`. Ordinary direct claims retain their multiple-possession policy.
The other no-acquisition outcomes are `nothing_ready` and `candidates_conflicted`;
the latter replaces the former client-only `lost_race` result. Fetch full work
context before working and resume any open handoff explicitly.

For an uncertain retry, reuse the exact key, credential and request. The run
may differ: a restarted session has a new run id, and its retry still replays.
Success and no-work outcomes are historical receipts, including the original lease
expiry and `active` value; replay never renews or reacquires. Use a new key for a
new polling intent, after reconciling any uncertain prior outcome. Completed
receipts expire (default 24 hours); reuse afterward is a new request. A crashed
request can remain indeterminate and must not be bypassed with a new key.
See [ATOMIC_PICKUP_SPEC.md](ATOMIC_PICKUP_SPEC.md) and
[OPERATIONS.md](OPERATIONS.md#durable-api-retry-keys) for the full retry contract.

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
