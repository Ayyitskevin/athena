# Work awaiting an answer is not ready

Date: 2026-11-22. This is the decision date.
Status: Accepted, owner instruction (D3).
Superseded-by: None.

## Context

[Atomic ready-work pickup](2026-11-17-atomic-ready-pickup.md) made selection
and acquisition one server command over the shared ready rule. That rule had no
idea of work that waits on a decision. The review of `99a0286` reproduced the
result as LOOP-1: an agent completes an issue with a report, which releases the
lease and leaves the issue open, and the next `claim_next_ready` returns the
same issue ahead of untouched work. The same followed a `needs_input` yield, and
the asker could then resume its own handoff and clear its question from the
operator's view without an answer. Requirement A2 of the
[agentic-use specification](../plans/AGENTIC_USE_SPEC_ATHENA.md) fixes it; its
decision D3 asked whether the hold should apply to every account.

## Decision

The ready rule in `src/athena/aegis/ready_queue.py` gains one clause, shared by
`claim_next_ready`, the desk's ready lane and its total, and `is:ready`.

- The issue's newest native settlement decides: its latest `claim_completed` or
  `claim_yielded` event.
- After a `needs_input` yield, the issue is ready for no account.
- After a completion, with or without a report, it is not ready for the account
  that completed it. Other accounts are unaffected.
- Either hold lifts when an answer is newer than the settlement: a comment, a
  status change, an assignment change or a claim on the issue, by an account
  other than the one that settled it and not the Automation actor.
- Imported history neither settles nor answers.
- `blocked` and `capacity` yields hold nobody back.
- Direct `claim_issue` is unchanged for every account. A direct claim by another
  account is itself an answer, and is how a `needs_input` handoff is taken over.

The desk lists each held-back issue in `work.waiting_on_operator.not_offered`
with its reason, so the ready total equals what pickup can serve.

## Consequences

The ready predicate in the atomic-pickup record is amended. That record's
command, receipts, replay, account guard and direct-claim policy stand.

An agent whose close is gated, or that leaves closing to the operator, moves on
to other work instead of being handed the same issue. The operator answers by
the ordinary acts: a comment, a status or assignment change, or a claim.

The account that settled the issue cannot release the hold by itself. Its own
comment does not count. Its direct claim does not count either, though the claim
still succeeds. A `needs_input` issue that nobody answers stays out of every
ready lane, and is visible only on the desk's `not_offered` list, in the
operator's open questions while the handoff is unresumed, and by direct read.

Readiness changing on an answer can let an account infer that an answer was
made on an issue it can see. It never names the answering account. The rule
adds no table and no migration. It adds two reads per candidate issue over the
existing `(target_kind, target_id, id)` activity index.

## Sources

- The owner accepted D3 as recommended on 2026-11-22, recorded in
  [Agentic-use specification: fifteen decisions accepted as recommended](2026-11-22-agentic-use-decisions.md):
  "After a `needs_input` yield, the issue is not ready for any account until it
  is answered. After a completion, it is not offered back to the account that
  completed it; other accounts are unaffected for now."
- [Agentic-use specification](../plans/AGENTIC_USE_SPEC_ATHENA.md), requirement
  A2; [review report](../plans/reports/repo-review-2026-11-21.md), finding LOOP-1.
- Implementation and tests: `src/athena/aegis/ready_queue.py`,
  `tests/test_ready_awaiting_answer.py`.
