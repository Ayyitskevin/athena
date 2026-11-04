# Approvals authorize an intent

Date: 2026-11-04 (recording an existing contract).
Status: Accepted.
Superseded-by: None.

## Context

Saving a refused mutation and replaying it after approval would separate the
action from the authorization and state checks that must apply when it executes.

## Decision

An approval binds the requester, action kind, and target. The requester retries
the ordinary command; current visibility, role, scope, policy, budget, and
If-Match checks still apply. Consumption commits with the successful write, so
a failed retry leaves the approval unspent.

## Consequences

Approval is single-use intent authorization, not approval of exact payload bytes,
deferred execution, or standing permission. An approved request may still be
refused on retry; the operator decision does not freeze the target's state.

## Sources

The accepted contract is [APPROVALS.md](../APPROVALS.md), implemented by
[approvals.py](../../src/athena/core/approvals.py) and the ordinary
[issue command](../../src/athena/aegis/issue_commands.py) retry path.
