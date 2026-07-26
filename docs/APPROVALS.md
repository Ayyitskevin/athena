# Human-in-the-loop approvals

`VISION.md`'s **Intervene** step promises the operator can "approve/reject risky
actions" and steer by exception. Before this, the only gate was the per-project
blocked-close policy (`WORKFLOW_GATES.md`), which can *refuse* but cannot *ask*:
there was no way for the operator to say "hold that one, I'll decide."

An **approval gate** makes a chosen action kind, for a chosen actor, require an
operator decision before it may happen.

## Gate + retry, not deferred execution

A gated command **refuses** and records a pending request naming who wanted to
do what to which target. The operator approves; the **agent retries the same
write** and it succeeds, consuming the approval.

Athena deliberately does **not** serialize the agent's mutation and replay it
later on the agent's behalf. Replaying a stored payload would re-execute it under
authorization evaluated at a different moment — a new trust boundary, not a
feature. Because the retry is an ordinary write, it re-validates *everything*:
visibility, role and scope, the blocked-close policy, the durable budget, and any
`If-Match`. **An approval authorizes an intent, never a stored side effect.**

The practical consequence: an approval that is granted but whose retry then fails
(budget exhausted, ETag stale, issue since blocked) stays **unspent** — the agent
can retry again once the other refusal is resolved.

## Single-use

Consuming happens inside the retry's own transaction, so the consumption commits
with the write it authorized. One approval, one action: an approval can never
decay into a standing permission. Closing the same issue again needs a new ask.

## Bound to requester *and* target

A live request is keyed on `(requested_by, action_kind, target_kind, target_id)`.
Approving agent A closing issue #7 does not let A close #8, and does not let
agent B close #7. A partial unique index enforces at most one live (pending, or
approved-and-unspent) request per intent, which is also what makes asking
idempotent: an agent retrying while it waits re-reads its existing ask instead of
flooding the operator's queue.

## Opt-in by default

**No policy row means ungated.** Applying migration 0063 changes nothing until an
operator gates someone — the same shape as budgets (0062) and the blocked-close
policy (0060). Nothing is gated by default; there is no "risky action" list
Athena decides on the operator's behalf.

## What can be gated

| Action kind | Fires on |
|---|---|
| `issue.close` | a write that transitions an issue from an open status into a done status (including one caused by a project move) |
| `dispatch.request` | handing an issue to the configured external executor (see [DISPATCH.md](DISPATCH.md)) |

The vocabulary is **closed**: an unknown action kind is refused at the boundary
(422) rather than stored as an inert policy row that silently gates nothing. It
lives in `core/approvals.py`, not in a schema `CHECK`, so adding a gate is a code
change with tests rather than a migration.

Each kind names **one intent**, and that is load-bearing: the operator approves
the intent they read on the ask, so two different actions must never share a
kind. Dispatch briefly borrowed `issue.close`'s policy row when it first
shipped, which meant gating an agent's closes silently gated its dispatches —
and an approval granted for *closing* an issue could be *spent* by a dispatch of
it. That coupling is gone; an operator who wants both gated sets both rows.

Deliberately **not** gated:

- **Automation rule firings.** Rules are the operator's own automation, not
  delegated agent work; the operator's automation must never wait on the
  operator. Rule firings go through the ungated command path
  (`enforce_actor_policy=False`), the same discriminator budgets use.
- Every other write. This is a bounded first slice, not a claim of total
  coverage.

## Guarantees

- **Atomic.** Consuming an approval happens inside the command's
  `BEGIN IMMEDIATE` transaction, so it commits or rolls back with the write.
- **Refusals leave nothing behind.** `ApprovalRequired` is raised *inside* the
  transaction, so the refused write unwinds whole. The request row is recorded
  afterwards on a freed connection — anything written inside that transaction
  would have rolled back with it.
- **Decisions are settled once.** Only a `pending` request can be decided;
  re-deciding a settled one is a 409 rather than a silent flip of an answer the
  agent may already have acted on.
- **Audited.** Every step records an activity event atomically with its change:
  `approval_requested`, `approval_approved`, `approval_rejected`,
  `approval_consumed`, `approval_policy_set`, `approval_policy_cleared`.
- **Part of the run story.** The ask carries the requesting run's `run_id`, so a
  pause for a human decision appears in the run's lineage rather than floating
  outside it.
- **Operator-only.** Reading the queue, deciding a request, and setting or
  clearing a policy are all admin-only. A gated agent cannot approve itself or
  ungate itself.

## Refusal contract

A gated action with no approval in hand:

```http
HTTP/1.1 202 Accepted

{
  "detail": "issue.close requires operator approval",
  "code": "approval_required",
  "approval": {
    "id": 7, "action_kind": "issue.close",
    "target_kind": "issue", "target_id": 42,
    "requested_by": 3, "run_id": "sol-1", "state": "pending",
    "decided_by": null, "decided_at": null, "decision_note": null,
    "consumed_at": null, "created_at": "2026-07-25T17:00:00Z"
  }
}
```

202, because the **ask** was accepted even though the action was not.

An action the operator explicitly rejected:

```http
HTTP/1.1 409 Conflict

{
  "detail": "issue.close was rejected by the operator: not this one",
  "code": "approval_rejected",
  "approval": { "id": 7, "state": "rejected", ... }
}
```

409 rather than 202 on purpose: **a rejection is an answer, not a delay.** An
agent that branches on `code` stops retrying instead of waiting for a decision
that already arrived. Branch on `code`, not on prose.

## Surfaces

```text
GET    /approvals?state=pending          # admin: the decision queue
GET    /approvals/{id}                   # admin: one request
POST   /approvals/{id}/decision          # admin: {"decision":"approve","note":"…"}
GET    /approvals/policies/{user_id}     # admin: gated action kinds for a user
PUT    /approvals/policies/{user_id}     # admin: {"action_kind":"issue.close"}
DELETE /approvals/policies/{user_id}/{action_kind}   # admin: ungate (idempotent)
GET    /users/me                         # carries your own "approval_required"
```

MCP: `list_approvals`, `decide_approval`, `set_approval_policy`, and the
`approval_required` field in `whoami`. The browser cockpit shows a **Waiting on
you** queue with approve/reject controls on **Admin → Agents**, alongside
per-agent gate toggles.

As with `scopes` and `budget`, an agent is expected to learn its gates by
**asking** (`whoami`), not by being refused.

## Limitations

- Two action kinds (`issue.close`, `dispatch.request`) are gateable today.
- Gates are per-user, not per-token or per-project: an agent holding several
  tokens shares one policy. The gate bounds the *actor*, not the credential.
- There is no expiry on a pending request and no reminder loop. An ask waits
  until the operator decides it; a queue nobody reads is a queue nobody answers.
- There is no bulk decide, and no "approve everything from this agent for the
  next hour" — that would be the standing permission single-use exists to
  prevent.
- A rejection is permanent for that exact intent: the operator ungates the actor,
  or the agent asks about a different target. There is no un-reject.
