# Review reports bind to one immutable candidate submission

Date: 2026-11-17.
Status: Accepted after independent specification review.
Superseded-by: None.

## Context

Free-form completion subjects and digests require the conductor to decide whether
a historical PASS covers the currently submitted build. The compact project brief
deliberately does not make that decision, and atomic pickup addresses acquisition
rather than review identity.

## Decision

Compose optional candidate submission or review association with the existing
completion command. The immutable canonical row references that exact native
completion event; the existing report remains its sole evidence/verdict owner.
An active own possession is required for these extensions. Reviews use separate
issues and accounts in the same project, and must name the latest candidate at
the serialized write boundary. Ordinary completion behavior remains unchanged.

Every submission receives a new ID, including A-to-A or A-to-B-to-A. A new
submission never inherits a previous review. Candidate replacement compares an
explicit predecessor only after checking its recorded visibility; hidden and
mismatched predecessors have the same refusal. Reads choose the latest canonical
records before visibility/parsing and never revive an older visible PASS.

Expose one bounded project queue and conditional full-context candidate history.
Keep the compact project brief and existing event/attention surfaces unchanged.
These labels are reported judgments against declared versions, not verified
external bytes, proof of independence, acceptance, approval, or execution authority.

## Consequences

There is one additive forward-only migration, no historical backfill, and no
live-data operation in this delivery task. Whole-database recovery retains review
associations; selective portability v1 excludes these operational rows and imports
completion reports only as foreign history. Restore a pre-upgrade snapshot with
the old build for rollback; there is no lossless downgrade claim.

This extends [completion reporting](2026-11-04-completion-reports.md) without
changing its no-authority contract or [intent approvals](2026-11-04-approval-intent.md).
It implements the separate review-freshness work identified in
[the project brief specification](../PROJECT_BRIEF_SPEC.md).

## Accepting sources

Kevin's task: “lets build the review queue tied to exact versions next. put together
the spec, get an independent review, then implement it and merge once all checks pass”.

[REVIEW_QUEUE_SPEC.md](../REVIEW_QUEUE_SPEC.md) carries the accepted contract.
Independent R2 reviewed SHA256:
`17f2e3219ad666b69866c76dbc41b0c94897d0553386ffaaabdf761ead19d41c`.
R1's hidden-predecessor equality disclosure was corrected before implementation.
The accepted repository copy changes only the reviewed status header.
