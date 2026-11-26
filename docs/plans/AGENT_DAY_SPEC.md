# The agent's day: less to read, the next move named, the loop closed

Date: 2026-11-26. Baseline: `13585e5` (`main` after Wave 10 of
[the in-use specification](ATHENA_IN_USE_SPEC.md)).
Status: **specified; decisions accepted by standing instruction; Wave 11 (AD1) built; Wave 12 under way (AD8, AD6 built); AD7 built.**
Evidence: [reports/agent-day-2026-11-26.md](reports/agent-day-2026-11-26.md).
Finding IDs such as `DAY-3` refer to that report. Requirement IDs begin `AD`.
Decision IDs continue the in-use specification's, from D29.

## Why this specification

The first two specifications made the agent contract true and made a fleet
workable. This one was written after the lead used Athena as an agent for one
working session instead of reading about it. Two agents were driven over MCP
from a fresh demo instance:

- a seat that answered an operator's steer, finished a claim, and picked up,
  questioned and closed a delegated slice;
- a conductor that wrote an outcome, sliced it, delegated it and answered the
  seat's question.

The protocol held at every step. A stale tag, a gated close and a retry each
told the agent its next move, and the agent got there. What cost the agent was
around the protocol:

- **reading** (DAY-1): about 63 KB of tool definitions before the first call,
  and a desk whose largest part is the same explanation on every read;
- **inferring** (DAY-2, DAY-6): what to do first, and which generation it
  holds;
- **an open delegation loop** (DAY-3 to DAY-5): the question never reached the
  delegator's desk, the answer did not come back with the question, and the
  slice did not show its contract;
- **guessing argument shapes** (DAY-7).

The objective is unchanged from the in-use specification. This document adds
one measure to it: **what one agent's day costs**, held by a test the way the
fleet trial holds an outcome.

## Principles

Principles 1 to 13 of the earlier specifications stand. Three are added.

14. **Say it once.** Text that is the same on every answer is served once per
    session, not with every answer. An answer carries the facts of this call.
15. **Name the next move.** Where Athena knows what the caller should do next,
    the answer names the call. That applies to a refusal, to the desk and to a
    question waiting on someone.
16. **The loop closes where it opened.** A question goes to the account that
    can answer it, and the answer comes back to the account that asked.

## Priorities

- **P0.** Without it the rest cannot be measured.
- **P1.** Every session or every loop pays it.
- **P2.** Friction an agent meets some days.

Each baseline cell ends with how it was established: *(lead)* measured by the
lead in the session the report describes, *(read)* confirmed by reading the
code or the document.

## Requirements

| ID | Pri | Baseline | Required behaviour |
|---|---|---|---|
| AD1 | P0 | The session in the report was a script in a scratch directory. Nothing holds its numbers. *(lead)* | `tests/test_agent_day.py` plays the report's session in process: a seat on `seat+knowledge`, a conductor on `conductor` and an operator on REST, over the real MCP tool layer. It records and holds under ceilings in `tests/agent_day_ceilings.json`: tool-listing bytes per profile, instruction bytes, full-desk bytes at the demo seed, work-context bytes (full and brief), calls per step, and total answer bytes per role. It has a scenario for each of AD4 to AD8, marked unmet as the fleet trial marks them. Each later requirement lowers its ceiling in the same commit. |
| AD2 | P1 | A full desk is 7,794 bytes, of which 3,560 (46%) is fixed explanation; `protocol` appears twice. A full work context is 7,545 bytes, with 1,284 fixed and 1,242 in null-valued keys. DAY-1. *(lead)* | Every `meaning`, `semantics`, `protocol`, `readiness` and `how_to_claim` text moves to one document an MCP session reads once: the resource `athena://answers/glossary`, which is also served at `GET /agent/glossary`. Each answer names its schema, and the glossary explains every key by schema. On the MCP surface, desk and packet answers drop null-valued keys and those texts by default, and `explain=true` restores them. REST answers are unchanged unless `?lean=true` is sent (D29). Targets at the demo seed: desk ≤ 4,300 bytes, work context ≤ 5,500 bytes. |
| AD3 | P1 | Before the first call a `seat+knowledge` session reads 62,758 bytes of tool definitions and a `conductor` session 67,165. `complete_claim`'s definition alone is 5,452 bytes. DAY-1. *(lead)* | A test holds the listing bytes per profile. Descriptions keep what decides the call: what it does, its refusals and the one next step. Protocol text that is already in the server instructions or a recipe moves there, named by a pointer. Targets: `seat` ≤ 34,000 bytes, `seat+knowledge` ≤ 45,000, `conductor` ≤ 48,000. No argument, scope or profile membership changes. |
| AD4 | P1 | Sol's first desk held an unanswered steer, a claim 29 minutes from expiry and a pending approval, in three sections among 7.8 KB. It ranked none of them. DAY-2. *(lead)* | The desk opens with `next`: at most five items from the same snapshot, each `{why, tool, arguments}`, ordered by a fixed rule written in `DESK.md`. The order is: a kill request; an unanswered run control; a handoff to resume; an own lease expiring within 10 minutes or already lapsed; a question asked of you (AD5); a review request; then the first ready issue. An empty desk says so and names `wait_for_events`. A test seeds each state alone and asserts the first item, then makes its call and asserts the call succeeds. |
| AD5 | P1 | Sol yielded Nova's delegated slice with a blocking question. It reached Sol's own desk and Nova's attention ranking, but not Nova's desk. The ranking's `next_action` was `operator-link` to a web page. DAY-3. *(lead)* | The desk gains `asks.questions`: open `needs_input` and `blocked` yields on issues the caller created, delegated or is assigned to, excluding the caller's own yields. Each row carries the issue, the asker, the blocking question, the handoff token and `next: comment_on_issue`. For an agent viewer, the attention row for that question names the same call as `agent-command`. The fleet trial's conductor answers from the desk and stops calling the ranking for this (D30). |
| AD6 | P1 | On re-claim the open handoff came back without Nova's answering comment, so Sol needed `list_issue_comments`. DAY-4. *(lead)* | An open handoff, wherever it is returned (`claim_next_ready`, `claim_issue`, the work context, the desk's handoff lane), carries `replies`. That is the newest three comments posted on the issue after the yield by any account other than the one that yielded, each as `{comment_id, author, excerpt, body_chars}`, with `replies_total`. |
| AD7 | P1 | The outcome had `## Goal` and `## Success criteria`. The slice's packet showed the parent's title only, and `work_order` reported all six parts missing. DAY-5. *(lead)* | A packet whose issue has a parent reports `work_order.inherited` from the nearest ancestor that has any of the six parts: `{from: {id, key}, present: [...], parts: {name: text}}`. Each part is bounded at 1,000 characters and carries `truncated`. The issue's own parts still win. This applies to the full and the brief packet. |
| AD8 | P1 | After claiming, the packet still said "get_issue_lease then claim_issue" and carried no generation. Four calls in the session were `get_issue_lease` made only to read it. DAY-6. *(lead)* | When the caller holds the issue's active lease, the full and brief packets carry `your_lease`: `{generation, expires_at, expires_in_seconds, open_handoff_token}`. `how_to_claim` is then replaced by `holding`, which names renew, complete and yield with the generation filled in. The agent-day ceiling drops by the four calls. *Built:* the lease's end is `until`, not `expires_at`, so the packet's tag stays a function of state; `how_to_claim` is kept and its `next_operation` names the three moves. The day's seat calls went from 22 to 20. |
| AD9 | P1 | `evidence` is a list of strings on `yield_claim` and a list of objects on `complete_claim`. The item shape of `results` exists only in prose; the schema says "any object". A wrong shape was refused "Input should be a valid list" with no example and no `next`. No evidence kind fits "I ran this command". DAY-7. *(lead)* | Every list-of-object argument has a typed item schema on MCP and in the agent OpenAPI document. An `invalid_arguments` refusal names each bad field, carries a minimal valid example of its expected shape, and names the same tool in `next`. A test makes the wrong-shape call for every such argument of every tool and validates the example it is given. `complete_claim` evidence gains the kind `command`: `{kind: command, command, outcome, scope}`, with the credential guard applied. `yield_claim` evidence also accepts that object form. Plain strings keep working on both (D31). |
| AD10 | P2 | Each `athena-mcp` process starts a new run, so one agent's work on one slice landed on three runs. `ATHENA_RUN_ID` exists but is not in the boot block, `RUNTIME_RECIPE.md` or any config Athena prints. `begin_run` is in no seat profile. DAY-8. *(lead)* | The desk's `identity` carries `last_run: {run_id, last_write_at}`, the caller's newest tagged write. The boot block and `RUNTIME_RECIPE.md` say how to continue it: set `ATHENA_RUN_ID`, or call `begin_run`. `begin_run` joins the seat profiles. Printed configs are unchanged; a run is per process unless the harness asks (D32). |
| AD11 | P2 | The `conductor` profile has no `list_projects`. A non-admin conductor cannot list seat slugs, and `assign_issue_to_seat` answered "unknown seat". `seat+knowledge` has `list_pages(space_id)` and no `list_spaces`. DAY-9. *(lead)* | `list_projects` joins `conductor`, and `list_spaces` joins `seat+knowledge`. A read on `conductor`, `list_fleet_seats`, returns each declared seat's slug, name and whether it has an agent account, to a member holding `issue:write`, with no keys or emails. The unknown-seat refusal names it in `next` (D33). |
| AD12 | P2 | One closed slice left the conductor 9 unread notifications, most of them approval bookkeeping on one issue. Nothing said the outcome's slices were all done. DAY-10. *(lead)* | `approval_consumed` notifies no one; it stays an event. `list_notifications` and the desk preview take `group=issue`: one row per target with its count, newest verb, newest id and `through_id` for one acknowledgement. When the last open child of an issue closes, that issue's creator and assignee receive one `children_done` notification. Grouping is the MCP default; REST keeps rows unless asked (D34). |
| AD13 | P2 | `complete_claim` and the closing `update_issue(status=...)` were always made together. The approval decision takes `approve` while its state reads `approved`. DAY-11. *(lead)* | `complete_claim` takes an optional `close_status`. After the release it applies that status through the same command as `update_issue`, approvals included: a gated close answers `approval_required` with the release already done and says so. The decision endpoint accepts `approved` and `rejected` as aliases (D35). |

## Scorecard

Measured at the demo seed by `tests/test_agent_day.py`, with ceilings in
`tests/agent_day_ceilings.json`. The baseline column is the test's first run at
`f1185b2`. It re-encodes answers as compact JSON, as the client receives them,
so the desk and packet read smaller here than in the report. The scripted seat
makes 22 calls where the report's hand-driven session made 37, because the
script never mistypes a shape or looks around.

| Measure | Baseline | Target |
|---|---|---|
| Tool-listing bytes, `seat` | 50,540 | ≤ 34,000 |
| Tool-listing bytes, `seat+knowledge` | 62,758 | ≤ 45,000 |
| Tool-listing bytes, `conductor` | 67,165 | ≤ 48,000 |
| Full desk bytes | 7,405 | ≤ 4,300 |
| Full work-context bytes | 6,980 | ≤ 5,500 |
| Seat calls for the day | 22 | ≤ 18 |
| Seat calls made only to read a generation | 2 | 0 |
| Seat calls to read the answer to its own question | 1 | 0 |
| Seat calls to read the slice's contract from its parent | 1 | 0 |
| Conductor calls to find a delegate's question | 1 (the ranking) | 0 (the desk) |
| Unread conductor notifications after one slice | 9 | ≤ 2 rows, grouped |

## Waves

One requirement ID is one change with its own commit, in the order given.

| Wave | Content | Uses decisions |
|---|---|---|
| 11. Measured | AD1 | — |
| 12. Less to read | AD8, AD2, AD3, AD6 | D29 |
| 13. The next move, and the loop closed | AD5, AD4, AD7, AD9 | D30, D31 |
| 14. Some days | AD10, AD11, AD12, AD13 | D32, D33, D34, D35 |

Wave 11 comes first, because every other row is a number it moves. AD8
precedes AD2 so that the packet's lease block is in place before the packet is
slimmed. AD5 precedes AD4 because `next` ranks the questions AD5 creates.

## Compatibility and deliberate exclusions

- Everything under this heading in the earlier specifications stands.
- REST answer shapes do not change by default. The lean shapes are the MCP
  default and an opt-in on REST (D29), and the desk schema becomes
  `athena.agent_desk.v3` only where the lean shape is served.
- `next` is advice computed from a snapshot, not a reservation or an
  authority. Each call it names makes its own checks.
- No question is answered, routed or reassigned automatically. AD5 shows a
  question to the accounts that can answer it.
- `children_done` closes nothing. Closing the outcome is still the conductor's
  act, and still gated if the conductor is gated.
- No new table. AD12's grouping is a read over existing notifications.
- Tool counts rise by `list_projects` and `list_fleet_seats` on `conductor`,
  `list_spaces` on `seat+knowledge` and `begin_run` on the seat profiles. Each
  is recorded in `scripts/surface_counts.toml` with what it earns.

## Decisions for the owner

On 2026-11-22 the owner told the lead to go with its recommendations and to
default to them at stopping points. Each recommendation below is recorded as
accepted on that standing instruction, and each is the owner's to reverse.

| ID | Decision | Recommendation (accepted by standing instruction) |
|---|---|---|
| D29 | Should desk and packet answers drop their fixed explanations and null keys by default? | Yes on MCP, where an agent pays for every byte. On REST only with `?lean=true`, because REST has consumers that read the current shape. The texts live in one glossary resource. |
| D30 | Who is asked a delegate's question? | The issue's creator, every account that delegated it, and its assignee, excluding the asker. These are the accounts that can answer it. The operator still sees every question in the attention ranking. |
| D31 | How are evidence shapes aligned without breaking callers? | Add the `command` kind, and accept object evidence on `yield_claim`. Plain strings keep working everywhere. No field is renamed. |
| D32 | Should a new MCP process continue the agent's last run automatically? | No. A run is one process unless the harness sets `ATHENA_RUN_ID` or calls `begin_run`, so two sessions of one account never share a run by accident. The desk shows the last run so continuing it is a deliberate choice. |
| D33 | May a member conductor read the declared seat slugs? | Yes: slug, name and whether an agent account exists, nothing more. Assigning to a seat already requires knowing one, and the project floor already shows a seated agent's slug to any member who can see the project. |
| D34 | Should notifications be grouped, and approval bookkeeping stop notifying? | Group on MCP by default and on REST when asked. `approval_consumed` stops notifying and stays an event. `children_done` is added, once per parent per time its children all close. |
| D35 | May `complete_claim` close the issue in the same call? | Yes, as an explicit `close_status` argument applied after the release through the normal update path, approvals included. Completing still never closes by itself. |

## Verification and delivery

Use `scripts/gate.sh --profile changed` while working, and
`scripts/gate.sh --profile product` on the wave's head, with the receipt in the
PR.

- Every requirement after AD1 moves at least one number in
  `tests/agent_day_ceilings.json` or removes an unmet mark, in the same commit.
- AD2, AD3 and AD12 change what every agent reads. Each also re-runs the
  contract agent and the fleet trial, and lowers their ceilings where the
  change removes bytes or calls.

A requirement is done when:
- the Definition of Done in `AGENTS.md` holds;
- its test fails without the change and passes with it;
- the agent-day scorecard row it names was re-measured;
- the documents that describe the behaviour were updated in the same PR;
- the "State of the project" section of the in-use specification points here.
