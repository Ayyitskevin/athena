# AI-assisted development in Athena

Athena is both a product for operating AI agents and a repository developed
with AI agents. That provenance is visible in branches, pull requests, commit
trailers, and repository-local instructions. This document explains what that
does—and does not—mean.

## Ownership model

Kevin is the product owner and final decision-maker. He owns the mission,
scope, risk tolerance, deployment decision, and whether a change belongs in
Athena. Agents may research, propose designs, implement bounded changes, write
tests, and perform adversarial reviews within an assigned branch.

An agent's output is a proposal until it passes the repository gates and lands
through a pull request. Model capability or token spend never substitutes for a
reviewable diff and reproducible evidence.

## Working model

- Each agent works in its own branch and checkout.
- `AGENTS.md`, not a transient chat prompt, defines repository behavior.
- One logical change belongs in one pull request.
- Requirements and non-goals are written before implementation when the slice
  changes a trust boundary.
- Tests explain the invariant and exercise the real database or transport where
  practical.
- Ruff, the full test suite, a real-process smoke, and artifact verification are
  objective gates.
- Security-sensitive work receives an adversarial pass that looks for bypasses,
  partial commits, data leakage, and misleading claims.

## Attribution

Pull requests disclose material AI assistance. Co-author trailers and session
references may be retained when they help reconstruct provenance. The human
owner remains accountable for accepting the result.

Athena does not claim that every line was typed manually, that every model
suggestion was independently rediscovered, or that a large test count proves
correctness. The credible claim is narrower: changes are scoped, inspectable,
tested against explicit contracts, and preserved in a review trail.

## Safety boundaries for agents

Agents must not:

- push directly to `main`;
- use production secrets or private user data in tests;
- invent UI data instead of using the database;
- silently broaden scope to make a task easier;
- weaken authorization to satisfy a failing test;
- describe browser, test-double, or Linux evidence as native/production proof;
- hide skipped checks or unresolved review findings; or
- merge unrelated cleanup into a security or correctness change.

## How reviewers can evaluate the process

Ignore the model name and inspect the evidence chain:

1. Is the requirement falsifiable?
2. Does one owner hold the mutation and its audit event?
3. Do tests fail for the bug or bypass being discussed?
4. Was the application or distribution artifact actually run?
5. Are residual risks and unsupported claims explicit?

The [`REVIEW_GUIDE.md`](../REVIEW_GUIDE.md) points to representative slices.
