# Coordination and history rulings

Date: 2026-10-20 (operator ruling; recorded here 2026-11-04).
Status: Accepted operator ruling.
Superseded-by: None.

## Context

Coordination and history cleanup are policy choices, not permission to erase
unfinished work or infer acceptance from a recommendation.

## Decision

The operator's ruling adopted these four constraints; the quoted substance is:

- **K1:** "no automatic lease sweep"; "Expired lease rows are never swept".
- **K2:** "no bulk acknowledgement"; existing notifications remain honest history,
  and recipients acknowledge what they actually handle.
- **K3:** "no retention clock this round" for `activity`, `activity_chain`,
  `run_bindings`, and `agent_run_checkins`; capacity pressure calls for visibility,
  not automatic pruning.
- **K4:** "coexistence/projection sorting CONFIRMED"; coexistence or projection
  is the working assumption for sorting proposals. The separate third class
  covers proposals that test a second store, projection surface, journals, habit
  automation, or ownership; those stop for the operator's decision.

## Consequences

Expiry does not authorize a cleanup sweep, viewing a notification does not
acknowledge it, and this ruling adds no history-retention timer. These are
operator policy constraints, not a claim that every cleanup API was removed or
that a proposal's category authorizes implementation; current command eligibility
still applies. A later policy change needs an explicit successor decision.

## Sources

Operator ruling of 2026-10-20, quoted above without deployment-specific details.
The public coordination contracts are [ACTIVE_WORK.md](../ACTIVE_WORK.md),
[AGENT_BOOT.md](../AGENT_BOOT.md), and [RUNS.md](../RUNS.md); those owners and the
[lease commands](../../src/athena/aegis/lease_commands.py) define the available
actions, rather than historical operational examples.
