# Decision records

These short records preserve accepted reasons and boundaries; the linked feature
documents and code own implementation details. A recommendation in
[DECISIONS_PENDING.md](../DECISIONS_PENDING.md) remains a proposal until accepted.
This index creates no runtime authority or automatic consumer.

| Date | Decision | Status |
|---|---|---|
| 2026-11-26 | [The owner's open decisions, made by delegation](2026-11-26-owner-decisions.md) | Accepted, owner delegation |
| 2026-11-22 | [A read that observes a lapsed lease records it](2026-11-22-lease-lapse-events.md) | Accepted, owner instruction (D11) |
| 2026-11-22 | [Athena in use: thirteen decisions accepted by standing instruction](2026-11-22-athena-in-use-decisions.md) | Accepted, owner's standing instruction |
| 2026-11-22 | [A review request carries what its requested reviewer reported](2026-11-22-requested-reviewer-verdict.md) | Accepted, owner instruction (D4) |
| 2026-11-22 | [A review request answers the reviewer's completion](2026-11-22-review-request-answers.md) | Accepted, owner's standing instruction (D16) |
| 2026-11-22 | [Work awaiting an answer is not ready](2026-11-22-ready-rule-awaiting-decision.md) | Accepted, owner instruction (D3); what answers a completion superseded in part, 2026-11-22 |
| 2026-11-22 | [Agentic-use specification: fifteen decisions accepted as recommended](2026-11-22-agentic-use-decisions.md) | Accepted, owner instruction |
| 2026-11-17 | [Saved title links retain page identity](2026-11-17-saved-title-links.md) | Accepted after independent spec review |
| 2026-11-17 | [Requesting an exact review](2026-11-17-review-requests.md) | Accepted after independent spec review; request verdict superseded in part, 2026-11-22 |
| 2026-11-17 | [Reviews bind to exact candidate submissions](2026-11-17-exact-version-reviews.md) | Accepted after independent spec review |
| 2026-10-20 | [Coordination and history rulings](2026-10-20-coordination-history.md) | Accepted operator ruling |
| 2026-11-04 | [Approvals authorize intent](2026-11-04-approval-intent.md) | Accepted existing contract |
| 2026-11-04 | [Preserve broken-chain recovery evidence](2026-11-04-recovery-chain.md) | Accepted, PR #421 |
| 2026-11-04 | [Completion reports carry no authority](2026-11-04-completion-reports.md) | Accepted, PR #423 |
| 2026-11-04 | [Refuse recognized credentials in new report writes](2026-11-04-report-credential-refusal.md) | Accepted implementation direction, maintainer request |
| 2026-11-17 | [Atomic ready-work pickup](2026-11-17-atomic-ready-pickup.md) | Accepted implementation direction, maintainer request; ready predicate superseded in part, 2026-11-22 |

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
