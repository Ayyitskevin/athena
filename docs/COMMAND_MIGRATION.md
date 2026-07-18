# Command-boundary migration

Athena's target architecture is **one command owns each write**. A command owns
domain authorization, validation, mutation, derived projections, and the audit
event in one SQLite transaction. REST and browser handlers translate transport
input/output; MCP uses REST.

This is a migration rule, not a completed-system claim. The inventory below is
the review-facing source of truth for the `0.1.0a1` line.

## Current inventory

| Area | Command-backed | Known migration debt |
|---|---|---|
| Issue core | Create; title/body/status/priority; assignee, project, and sprint placement; typed dependency link/unlink | Parent hierarchy, archive/restore, labels, contributors/delegation, comments |
| Projects | Issue placement uses the issue command | Project create/edit/delete, visibility, and membership |
| Mentor | None of the page/space lifecycle is presented as command-complete | Space and page create/edit/move/delete/restore, visibility, membership, labels, and comments |
| Users and agents | User create, role/agent flag changes, token kill switch, and complete offboard | Password/session preference operations remain separate bounded flows |
| API tokens | Mint and revoke | None known in the token lifecycle |
| OIDC identities | Link and unlink | Provider exchange/discovery are transport/service flows rather than durable domain writes |
| Webhooks | Register, pause/resume, and delete | Delivery cursor/health updates are operational state and intentionally owned by the delivery subsystem |
| Automation | Rule create, enable/disable, and delete; core issue edits dispatched by a rule | Some rule actions still compose legacy label/comment/contributor writes |
| Agent runs | Run/check-in operations use their dedicated command and run-context owners | General pause/kill/budget/approval controls are roadmap work, not shipped guarantees |

The table identifies ownership shape, not test coverage or security severity.
Before changing a listed legacy path, inspect both REST and browser adapters;
some pairs already share lower-level validation while still split the mutation
from activity emission.

## Rules for migration slices

A migration is complete only when:

1. the command accepts a resolved actor rather than trusting a bare actor id for
   privileged work;
2. authorization and visibility checks execute inside the command boundary;
3. the domain write, projections, notification facts, and activity event commit
   or roll back together;
4. REST and browser adapters call the same command;
5. MCP reaches that REST behavior without a parallel mutation;
6. no-op and idempotent behavior is explicitly defined; and
7. tests cover success, rejection, rollback, attribution, and transport parity.

## Why migration is incremental

Athena is a working local alpha with broad route coverage. A flag-day rewrite
would create a large, hard-to-review authorization diff. Vertical slices keep
the system runnable and make each trust-boundary change independently testable.
New writes must use commands immediately; legacy paths move when touched or when
their risk ranks them ahead of product work.
