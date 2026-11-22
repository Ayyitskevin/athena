# Build brief template

Use this to brief a build agent on one or two requirements of a specification
under `docs/plans/`. It has six parts. A brief that leaves one out sends the
agent to guess it.

The brief is the only context the agent has. Point at files and documents
instead of retyping them, and say what has already been ruled out.

```text
GOAL
One sentence: the behaviour that will be true when this is done, and the
requirement ID it satisfies.

CONTEXT
- Repository contract: AGENTS.md. Read it first.
- The specification and the requirement's row: docs/plans/<spec>.md, <ID>.
- The evidence: the report section and finding ID, and how the baseline was
  established (re-run, read, or reported once).
- Decisions this relies on, by ID, and what each allows.
- What an earlier wave already built that this must use, not rebuild.
- What was tried or ruled out, and why.

INPUTS
- Files to read before changing anything, by path.
- The script or request that reproduces the baseline, with its output.
- The commit the work starts from.

ACTION
- Write the failing test from the baseline first and confirm it fails.
- Make the change. One command owns each write; refusals carry a code.
- Stay inside the requirement. List what you found and did not fix.
- Do not commit, push, tag, or touch docs/DECISIONS_PENDING.md, the prune
  ledger's verdicts, a coverage floor or ALLOWED_PRAGMAS.

FORMAT
- Leave the work uncommitted in the checkout.
- Write one patch per requirement to <scratch path>, such that applying them
  in order to the starting commit reproduces the tree.
- Report: what changed, by file; the test that fails without it; every
  command you ran with its closing lines; what you measured before and after;
  what you found and left; anything you are unsure of.

SUCCESS CRITERIA
- The new test fails on the starting commit and passes on the change.
- scripts/gate.sh --profile changed passes; quote its closing lines.
- The scorecard row, if the requirement has one, is re-measured.
- The documents that describe the behaviour are updated in the same patch.
- Nothing outside the requirement changed.
```

A reviewer's brief uses the same six parts with a different action: change
nothing, run against a throwaway instance, and label each finding as
reproduced, read from code, or suspected.
