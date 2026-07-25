# Blocked-issue close policy

Athena projects can opt into a hard workflow gate that protects issues with
unresolved blockers. The setting is **disabled by default**, so migration 0060
does not change existing close behavior until a human operator enables it.

## What counts as blocked

The policy uses Athena's canonical dependency rule. An issue is blocked when an
incoming stored `blocks` edge points from another issue whose status category is
not `done`. Each blocker is interpreted through its own project's status set, so
custom done-category statuses such as `shipped` or `verified` resolve the blocker.
Athena does not maintain a second policy-specific blocker definition.

A refusal never includes blocker ids, keys, titles, or counts. Read surfaces may
show only blockers visible to that viewer; an empty visible list is not proof that
no hidden blocker exists.

## Configure a project

A human project creator or human admin can configure the policy from
**Project → Access → Agent close policy**. Agent identities cannot configure it,
even if an agent owns the project or has the admin role.

REST configuration is an optimistic-concurrency write:

1. `GET /projects/{id}` and retain its strong `ETag` response header.
2. `PUT /projects/{id}/policy` with that value in `If-Match` and this body:

```json
{"block_agent_closes_when_blocked": true}
```

Missing, malformed, wildcard, or stale validators fail without changing the flag.
A real change and its `project_blocked_close_policy_changed` audit event commit in
one transaction. Repeating the current value is an idempotent no-op: no new event.

## Enforcement and override

The shared issue command evaluates the policy inside the same immediate transaction
as the issue ETag, status/project mutation, lifecycle events, notifications, and
run lineage. REST, browser, bulk, board, MCP, delegated-agent, and automation writes
therefore cannot develop separate enforcement rules.

When the policy is enabled and canonical blockers remain:

- an agent cannot move the issue from a non-done category into any done category;
- forged override flags and an agent admin role do not bypass the refusal;
- event automation records a rule failure, and scheduled automation retains its
  bounded retry/failure receipt instead of treating the refusal as a no-op;
- an agent cannot move the issue from a protected project to an unprotected project
  or backlog as a two-step escape; harmless entry into protection and moves between
  protected projects remain allowed;
- an eligible human issue writer must explicitly acknowledge the override.

A human REST override uses `PATCH /issues/{id}` with the reviewed issue `If-Match`:

```json
{"status":"done","override_blocked_close":true}
```

The focused browser issue page exposes the equivalent confirmation. Bulk, board,
MCP, and automation surfaces deliberately do not offer an override control. A
successful exception appends `overrode_blocked_issue_close` beside the normal
status transition under the same actor and run lineage. Supplying the flag when no
protected blocked close occurs has no effect and creates no override event.

A denied REST or MCP write returns HTTP 409 with a stable, non-leaking body:

```json
{"detail":"blocked issue close policy denied this update","code":"blocked_issue_close_policy"}
```

Bulk results carry the same code only for affected items. Browser and board paths
show a generic refusal without revealing hidden blocker content.

## Recovery

Choose one deliberate recovery path:

1. Complete every blocker into a done-category status, then retry.
2. Remove an incorrect dependency edge through the normal audited unlink command.
3. Have an eligible human issue writer use the explicit audited override.
4. Have a human project creator/admin disable the project policy from a freshly
   reviewed project ETag, then retry.

Disabling the policy does not change dependencies or issue status; it restores the
pre-0060 close behavior for that project.

## Migration and limits

Migration `0060_project_blocked_close_policy.sql` adds one checked boolean column
with a default of zero. Athena migrations are forward-only. Before upgrading a
material database, take the normal matched database/attachment backup. Rolling
application code back across 0060 is not a schema downgrade; restore the pre-upgrade
database backup with the older build.
Project portability bundles carry the policy flag and restore it transactionally.
Older V1 bundles that do not contain the additive field remain compatible and
import with the safe historical default of disabled.


This is a per-project close gate, not a transition graph, approval engine, or
multi-worker coordinator. It adds no external service and does not change Athena's
single-process automation boundary.

## Relationship to approval gates

This policy and the per-actor approval gates in [`APPROVALS.md`](APPROVALS.md)
are separate opt-in mechanisms that answer different questions: this one asks
"may this issue be closed at all right now?", approvals ask "may *this actor*
close it without me?". A close evaluates the blocked-close policy **first** —
there is no point asking the operator to approve something the project forbids
outright — so a refusal here is never converted into a pending approval request.
