# The owner's open decisions, made by delegation

Date: 2026-11-26. This is the decision date.
Status: Accepted, owner delegation; each is the owner's to reverse.
Superseded-by: None.

## Context

Three items in [DECISIONS_PENDING.md](../DECISIONS_PENDING.md), the twenty
verdicts in the [prune ledger](../PRUNE_LEDGER.md), and six questions the
[Athena in use specification](../plans/ATHENA_IN_USE_SPEC.md#decisions-for-the-owner)
left with the owner were all waiting on one person. Each had been held back from
agents on purpose: they change what Athena claims, or what it promises users.

On 2026-11-26 the owner asked the lead, in the working session, to choose the best
answers on their behalf, acting as an advisor who wants to finish the project with
them. That instruction is the accepting source for everything below. Where a brief
already carried a recommendation, the recommendation was taken; every choice is the
safe, reversible one, and each says what would reverse it.

## Decision

**1. Transport encryption is the tailnet's, permanently (F-1.2, option b).** Athena
serves plain HTTP inside WireGuard/Tailscale and claims no TLS or proxy shape.
Recorded in [SECURITY.md](../../SECURITY.md#transport-encryption-is-the-tailnets-job);
the launcher's `ATHENA_COOKIE_SECURE` refusal now names that section. *Reverse when*
a real deployment is blocked on a proxy: bless one recipe then, chosen by that
deployment.

**2. Recovery is Linux-only (F-3.3, option b).** No portable primitive publishes a
directory atomically without clobbering, so a port trades away the guarantee the
recovery path exists for. Recorded in [OPERATIONS.md](../OPERATIONS.md#supported-platform);
`athena-doctor` warns in every mode on a platform without Linux, procfs or
`renameat2`. *Reverse when* someone needs recovery off Linux: the symlink swap is
the one substitute that keeps atomicity, scoped as its own item.

**3. No container image is published (F-2.3, option b).** The Dockerfile ships and
CI keeps proving it; users build locally. Recorded in
[DOCKER.md](../DOCKER.md#no-image-is-published--build-it-yourself). *Reverse when*
decision 1 changes or someone asks for the image by name: a job in `publish.yml`.

**4. Every prune verdict is keep; four are watched.** Keep changes nothing and
costs nothing to undo, and four subsystems still have no usage evidence either
way. Automation rules, outbound webhooks, the graph view and the answerability
page are kept on a watch list, each with the evidence that would park it at the
2026-Q4 review. Recorded in the [prune ledger](../PRUNE_LEDGER.md).

**5. Assignment refuses an account that cannot see the issue's project.** Today it
succeeds and answers with that account's name, which the eligible-accounts read
(D19) deliberately does not list. Assigning work to someone who cannot read it is
never useful, and the answer discloses a name. The refusal names no account: it is
the same answer an unknown account gets. Delegation and a contributor add apply
the same rule, so the eligible-accounts list and every write that takes an account
are one rule (`issue_commands._eligible_target`; `tests/test_eligible_accounts.py`).

**6. An administrator can remove a kept prior text.** F10 keeps what an edit
replaced, so editing a secret out of an issue no longer removes it. An admin-only,
audited removal of one prior version answers that: the trail records who removed
which version and when, never its text. Built as
`DELETE /issues/{issue_id}/prior-texts/{event_id}` (`aegis/prior_text_commands.py`,
migration 0096; `tests/test_prior_text_removal.py`); REST only, since the cockpit
never shows a prior text and erasing record is not an agent's act.

**7. The operator CLI, demo and smoke script act as the account they record.** The
next command-debt payment needs these entry points to resolve an actor rather than
pass a bare id; each keeps acting as the account it records today, so nothing they
write changes owner. Built: `token_commands.mint_token` and `revoke_token` take the
resolved account and re-read it under their lock, and the ceiling is 35.

**8. The three missing twins are built.** Assigning to a fleet seat over REST,
resuming a claim handoff in the cockpit, and editing a sprint in the cockpit; each
deletes its `missing-twin` row in `scripts/surface_parity.toml`. Assigning to a
seat is built as `POST /issues/{issue_id}/assign-seat` and the conductor's
`assign_issue_to_seat` tool (`workflows/fleet_assign_api.py`;
`tests/test_fleet_assign_api.py`). Resuming a handoff is the palette's "Resume
handoff", offered to the current leaseholder while a handoff is open
(`web/palette.py`; `tests/test_palette_resume_handoff.py`). Editing a sprint is
the sprint list's "Edit" (`POST /aegis/sprints/{sprint_id}/edit`;
`tests/test_sprint_edit_web.py`).

**9. The root catalog's digest and root keys stay withheld from agents.** Nothing
has changed the reason `ACTIVE_WORK.md` gives (operator evidence first), and
keeping a secret withheld is the reversible direction.

## Not decided here

Three questions need facts only the owner has, so they stay open rather than be
guessed: what the name "Astra" refers to in the lanes; whether the fallback rules
in `AGENTS.md` match the machine-level handbook, which is not on the machine these
lanes run on; and D9's precondition, an instance someone operates, which keeps B11
blocked. The `ATHENA_ANONYMOUS_READS` default (D23) and cutting a tag remain the
owner's acts with the next release.

## Consequences

`AGENTS.md` still forbids an agent from setting a prune verdict or answering a
pending decision on its own; this record is the owner's delegation, not a change to
that rule. Future quarterly reviews set verdicts the same way: by the owner, or by
the owner's explicit delegation, cited.

## Sources

- The owner's instruction in the working session, 2026-11-26.
- [DECISIONS_PENDING.md](../DECISIONS_PENDING.md) briefs 1 to 3 and their
  recommendations.
- [PRUNE_LEDGER.md](../PRUNE_LEDGER.md) evidence, including the 2026-08-22 live
  snapshot.
- [ATHENA_IN_USE_SPEC.md](../plans/ATHENA_IN_USE_SPEC.md), "Decisions for the
  owner" and "Next step".
