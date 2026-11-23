# The Office — one chair per agent

Athena's unique agent surface. The **desk** is the whole board (asks, inbox,
signals, cursor). The **office** is the cubicle: you sit in at most one chair,
you work only the fenced paths, and you stand up without pretending the issue
is done.

```text
GET  /office          → the cubicle
MCP  my_office()
GET  /desk            → the board, which now includes `office`
```

## Why this is not a ticket list

Other tools give agents a queue. Athena gives an agent a **seat**:

| Idea | Means |
|------|--------|
| Chair | Exactly one *active* lease, or you are standing |
| Fence | `declared_paths` on that lease (empty = issue only) |
| Checkout hint | Branch name `athena/<issue-key>-<seat-slug>` — Athena does not create remotes |
| Stand up | `complete_claim` — lease gone, issue status unchanged |

Two agents can still read the same office shape at once. Sitting down is
`claim_issue`. The office **reserves nothing**.

## Packet

```json
{
  "schema": "athena.office.v1",
  "seat_slug": "grok",
  "seated": true,
  "chair": {
    "issue_id": 1,
    "issue_key": "MWS-1",
    "generation": "…",
    "declared_paths": ["src/athena/aegis/desk.py"],
    "checkout_hint": "athena/mws-1-grok"
  },
  "next_to_sit": null,
  "protocol": { "claim_one_issue": true, "do_not_touch_other_work": true },
  "fence": { "basis": "lexical, root unknown" },
  "warnings": [],
  "fence_overlaps": []
}
```

`seat_slug` is the same identifier as Buzz, systemd (`buzz-acp-grok`), and
`drift-check`. Undeclared accounts get `null`.

`seated` is true only when there is **exactly one** active lease. Two active
leases set a warning: pick a chair and release the other.

## Fences

A fence is the `paths` a lease declares (`claim_issue`, or `claim_next_ready`,
which declares them on the claim it makes). `fence.basis` in the office says how
this instance compares them:

| `fence.basis` | When | How paths are compared |
|---|---|---|
| `lexical, root unknown` | No root catalog (`ATHENA_LEASE_ROOT_CATALOG` unset), the default | As written, under one implicit root, across every project |
| `same root` | A root catalog is configured | Within one catalog root's tree, after its prefix; distinct trees never overlap |

The basis therefore shows whether a catalog is configured (decision D5), and
nothing more: no read returns the catalog's digest, root keys or contents. With a
catalog, a `coordination_root` selector (`{"catalog_sha256", "root_key",
"relative_prefix"}`) comes from the operator through the qualification workflow
in [ACTIVE_WORK.md](ACTIVE_WORK.md#qualified-declared-work-roots). A lease's
`path_fence` says the same per lease: `rooted`,
`lexical, root unknown`, `unresolved` (with a catalog: no current root, or the
stored paths are unreadable) or `issue_only` (no paths).

**Normalisation and comparison.** `./`, empty segments and a trailing `/` drop, `\`
becomes `/`; a path with `..` or a leading `/` is refused as invalid input (422),
never compared. Two paths overlap when they are equal or one is a directory of the
other, segment by segment: `src/app` covers `src/app/x.py`, not `src/application`.
Every active lease takes part, in every project, including ones you cannot see. A
lease that expired or was completed fences nothing. A renewal is not compared with
its own lease.

**The refusal.** An overlap is `409 fence_overlap`; its `next` is
`claim_next_ready`. A fence that cannot be placed (with a catalog: either fence has
no current root, or your `coordination_root` is not in the catalog; without one: a
`coordination_root` was sent at all) is `409 fence_unresolved`. No tool call
resolves it, so it has no `next`; its detail says what to do: without a catalog,
omit `coordination_root`; with one, obtain a current reviewed selector from the
operator. Both carry `fence`:

```json
{"detail": "declared path src/app/x.py overlaps another active lease's fence (lexical, root unknown)",
 "code": "fence_overlap",
 "fence": {"basis": "lexical, root unknown", "path": "src/app/x.py",
           "issue": {"id": 12, "key": "FLT-3"}, "holder": {"id": 5, "name": "Ada"},
           "holder_path": "src/app"}}
```

`path` is yours (null for `fence_unresolved`). `issue`, `holder` and `holder_path`
are filled only when you could read that lease (`GET /issues/{id}/lease`);
otherwise they are null and the body is otherwise identical, so a hidden holder
is never disclosed. `claim_next_ready` whose paths are refused claims nothing and
answers `claimed: false` with `reason` `fence_overlap` or `fence_unresolved` and
the same `fence`; the verdict does not depend on the candidate, so it stops at the
first. Claiming without paths reserves no files and is unchanged.

## Overlaps already standing

`fence_overlaps` lists every other live lease whose declared paths overlap one
of yours, on an issue you can see. Each entry gives the other issue and holder,
both paths, and the basis of the comparison (`same root`, or `lexical, root
unknown`). Each one also adds a warning line: `your fence overlaps issue X, held
by Y`.

**How paths are compared.** As above. A stored `..` path makes that fence
unreadable rather than collapsed. With a catalog, two fences with no current root
are compared as written here too, as a warning, not proof.

**It is shown, never enforced.** The claim command's overlap check stays the only
gate. An overlap can exist only where something bypassed that check, for example
a root catalog that changed under live leases.

**Where the operator sees it.** The attention card counts the pairs, and each
pair is a row in its Now queue linking both issues and naming both holders.

## The floor

A **project** is a branch office. `GET /aegis/projects/{id}/floor` (HTML),
`GET /projects/{id}/floor` (REST / MCP `get_project_floor`) lists every
*open* issue as a chair. Occupied is **derived** from an active lease — never
stored on the floor. Empty = still needs a body. `blocked_by` is the existing
issue-link graph (open blockers only). There is no ready flag; `claim_issue`
plus If-Match remains the authority.

**Rooms** are flavor. Warehouse / Accounting / Sales / Annex group chairs
and filter the floor (`?room=warehouse`). They do not change claim, lease,
status, or assignee. Unplaced chairs live in The Annex. Stock the starter
pack once, or add your own rooms.

This is how many agents share one big project without sitting in the same
chair. An operator who can write may sit a declared seat in an empty
chair from the floor (`POST .../floor/assign`) — same command as Admin →
Fleet assign, including the optional Buzz radio.

Blockers for the whole floor are loaded in a handful of queries, not one
round trip per chair.

## Operator view

Admin → Fleet → **Who is sitting** lists active chairs. That is occupancy,
not liveness.

## What the office will not become

- A scheduler or process runner
- A git host
- A second chat
- A claim that the agent is "alive"
