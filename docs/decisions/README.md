# Decision records

These short records preserve accepted reasons and boundaries; the linked feature
documents and code own implementation details. A recommendation in
[DECISIONS_PENDING.md](../DECISIONS_PENDING.md) remains a proposal until accepted.
This index creates no runtime authority or automatic consumer.

| Date | Decision | Status |
|---|---|---|
| 2026-10-20 | [Coordination and history rulings](2026-10-20-coordination-history.md) | Accepted operator ruling |
| 2026-11-04 | [Approvals authorize intent](2026-11-04-approval-intent.md) | Accepted existing contract |
| 2026-11-04 | [Preserve broken-chain recovery evidence](2026-11-04-recovery-chain.md) | Accepted, PR #421 |
| 2026-11-04 | [Completion reports carry no authority](2026-11-04-completion-reports.md) | Accepted, PR #423 |

Use a dated filename and these plain fields: **Status**, **Context**, **Decision**,
**Consequences**, **Sources**, and **Superseded-by**. State whether the date is the
decision date or the date an existing contract was recorded; cite the accepting
source rather than inferring acceptance from a recommendation or implementation
idea. Unaccepted choices stay Proposed.

When a consequential decision changes, add a successor, mark the old record
Superseded, and fill its Superseded-by link; retain its context and sources. Use
None while a record has no successor. Documentation records reasoning, not
permission to skip authorization or verification. See the [glossary](../GLOSSARY.md)
for the terms used here.
