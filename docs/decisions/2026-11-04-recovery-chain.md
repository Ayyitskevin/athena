# Preserve broken-chain recovery evidence

Date: 2026-11-04.
Status: Accepted, PR #421.
Superseded-by: None.

## Context

Refusing capture solely because an activity chain is broken would discard the
database and matching blobs needed for investigation or emergency recovery.

## Decision

Capture publishes the otherwise valid recovery bundle despite a chain mismatch,
with a warning; other capture failures still prevent publication. Strict bundle
verification refuses a broken chain. Only an explicit operator emergency drill
may use `--accept-broken-chain`, together with `--recovery-bundle` and `--drill-dir`.
Automated jobs must not use that exception.

## Consequences

Preserved evidence is not a verified recovery point. The emergency scratch copy
keeps the failed chain result visible and neither repairs nor rechains it; the
flag does not certify the bundle or replace a live deployment.

## Sources

PR #421's contract is documented in [OPERATIONS.md](../OPERATIONS.md) and
implemented in [recovery.py](../../src/athena/core/recovery.py) and the
[doctor CLI](../../src/athena/ops.py).
