# Completion reports carry no authority

Date: 2026-11-04.
Status: Accepted, PR #423.
Superseded-by: None.

## Context

Releasing a claim must allow an honest report of unfinished work and uncertainty,
without forcing a false success claim or inventing an independent verdict.

## Decision

Completion may record conditions as `met`, `not_met`, `not_assessed`, or
`inconclusive`, with unfinished work, uncertainty, and inert evidence references.
These are the actor's reports. References are not resolved, fetched, or executed,
and there is no overall success boolean or score.

## Consequences

A negative or unrun report does not itself prevent ordinary lease release; the
lease-generation and handoff rules still apply. Reporting does not change issue
status, grant or consume approval, satisfy a gate, or establish that work passed.
Actual verification remains separate, and copying the report format into
imported history cannot create a native reported completion.

## Sources

PR #423's contract is [CLOSEOUT_RECIPE.md](../CLOSEOUT_RECIPE.md), implemented in
[lease_commands.py](../../src/athena/aegis/lease_commands.py) and
[issue_activity.py](../../src/athena/aegis/issue_activity.py).
