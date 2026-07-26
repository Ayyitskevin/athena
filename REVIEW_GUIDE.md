# Athena peer-review guide

Athena is **mission control for a one-person AI fleet**: one self-hosted
workspace where an operator gives agents work and context, observes their
actions, intervenes, and later reconstructs what happened.

This guide provides a bounded review path. It is more useful to challenge one
vertical slice deeply than to skim every feature.

## Five-minute orientation

1. Read [`docs/VISION.md`](docs/VISION.md) for the operator loop and non-goals.
2. Read the principles and layout in
   [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
3. Run the disposable demo:

   ```bash
   python -m venv .venv
   .venv/bin/python -m pip install \
     -c constraints/ci-py312.txt -e ".[dev,mcp]"
   .venv/bin/athena-demo --db /tmp/athena-review.db
   ```

4. Sign in with the disposable credentials printed by the command and inspect:
   **Dashboard → issue → activity → agent cockpit → Mentor page**.

The demo writes only to the new database and attachment directory it names,
binds to `127.0.0.1`, disables outbound/background automation, and refuses to
overwrite an existing database.

## Thirty-minute technical tour

### 1. One audited write

Start with [`src/athena/aegis/issue_commands.py`](src/athena/aegis/issue_commands.py).
Follow issue creation or editing through:

- REST in `src/athena/aegis/api.py`;
- browser transport in `src/athena/web/router.py`;
- the shared command and SQLite transaction;
- activity, links, search, and notification projections; and
- `tests/test_issue_commands.py` plus authorization tests.

Review question: **Can any transport produce a different authorization,
mutation, or audit outcome for the same command?**

### 2. Agent control boundary

Inspect [`src/athena/core/agent_commands.py`](src/athena/core/agent_commands.py)
and `tests/test_agent_kill_switch.py`. Trace role and token-scope authorization,
atomic token/session revocation, last-admin protection, and audit attribution.

Review question: **Does a compromised or read-scoped agent have any path around
the operator's kill switch or the command's authorization check?**

### 3. Visibility-safe context

Read [`docs/WORK_CONTEXT.md`](docs/WORK_CONTEXT.md) and
`src/athena/aegis/work_context.py`. The packet is deliberately bounded and
actor-visible; missing warnings do not assert readiness.

Review question: **Can hidden nested data leak through items, counts, clipping,
warnings, ETags, or timing-dependent composition?**

### 4. Active-work supervision

Read [`docs/ACTIVE_WORK.md`](docs/ACTIVE_WORK.md),
`src/athena/aegis/fleet_work.py`, and `tests/test_fleet_active_work.py`. Trace one
claim through its exact run event, cooperative check-in, current holder controls,
blockers, and replay evidence. Force project access or credentials to drift after
the claim and confirm the view reports attention without claiming process health.

Review question: **Can any mutable holder, lease, visibility, blocker, or reporting
fact make Athena say an agent is eligible, healthy, running, or unblocked when the
underlying command boundary would disagree?**

### 5. Historical throughput evidence

Read [`docs/FLEET_METRICS.md`](docs/FLEET_METRICS.md),
`src/athena/aegis/fleet_metrics.py`, migration `0055_issue_lifecycle_facts.sql`,
and `tests/test_fleet_metrics.py`. Trace a create, completion, reopen, and
reclosure from the issue command into its typed activity fact, then through the
shared visibility predicate and bounded admin cycle projection. Confirm partial-
visibility roles never inspect hidden predecessor availability.

Review question: **Can mutable status/actor state, imported history, a hidden
project, an orphan target, or an evidence cap silently change a headline,
median, actor row, coverage signal, or no-data state?**

### 6. Adversarial outbound networking

Read `src/athena/core/webhooks.py` and `tests/test_webhook_ssrf.py`. Focus on
redirect refusal, DNS pinning, split answers, embedded IPv4 forms, TLS hostname
verification, and failure isolation.

Review question: **Can registration-time validation and connection-time behavior
disagree in a way that reaches an internal address?**

### 7. Distribution evidence

Read [`.github/workflows/ci.yml`](.github/workflows/ci.yml),
`scripts/verify_wheel.py`, and `scripts/smoke_app.py`. CI builds through an
extracted source distribution, compares runtime assets, installs the wheel, and
boots it outside the checkout.

Review question: **Does the shipped artifact prove the same behavior as the
editable checkout?**

## Feedback requested

The highest-value feedback is about:

1. whether the command/transaction boundary is coherent and consistently used;
2. whether visibility and token scopes fail closed;
3. whether SQLite plus one process is honest for the stated 1–5 person scale;
4. whether tests verify behavior rather than reproduce implementation details;
5. which complexity should be removed before the first dogfood deployment.

Please identify the exact file, invariant, and failure mode. Broad feature
requests are less useful than one falsifiable architectural challenge.

## Claims this alpha does not make

Athena is not a public multi-tenant service, an enterprise permission system,
a real-time collaborative editor, or a general workflow engine. General undo and
complete command ownership are roadmap goals — undo by compensation ships for four
reversible verb pairs ([`docs/UNDO.md`](docs/UNDO.md)), not for every write. Durable per-agent action budgets
([`docs/AGENT_BUDGETS.md`](docs/AGENT_BUDGETS.md)) and human-in-the-loop approval
gates ([`docs/APPROVALS.md`](docs/APPROVALS.md)) are implemented, both opt-in and
both deliberately narrow — budgets meter a handful of writes, and only
`issue.close` and `dispatch.request` are gateable action kinds. The worker registry's kill is **cooperative**
([`docs/WORKERS.md`](docs/WORKERS.md)): Athena records the request and the
worker's reply, and can neither signal nor observe a process — a silent worker is
stale, never terminated. See
[`docs/COMMAND_MIGRATION.md`](docs/COMMAND_MIGRATION.md) for the remaining split
write paths.
