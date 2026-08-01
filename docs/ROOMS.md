# Athena Rooms v1

Status: Rooms v1 implementation and design contract.

Rooms make one Athena project legible as an operator/agent workspace without
turning Athena into chat or execution infrastructure. The design follows
[ADR 0001](adr/0001-rooms-coordination-substrate.md).

## Success criteria

An authorized operator can open a project's main room, an issue room, an agent
room, or a live brief and determine what the work is, who is involved, what
changed, what is blocked, what awaits approval, what was decided, and where the
authoritative records live. An agent contributes only as its authenticated
Athena actor. Asking a question returns a bounded evidence packet with receipts,
not a generated answer.

## Ownership and schema

Rooms remain an Aegis project concern:

- `aegis.rooms` owns room reads and projections;
- `aegis.room_commands` owns every room/event write and its transaction;
- `aegis.rooms_api` and `web.rooms` are REST and HTML adapters;
- MCP calls the REST contract and never writes in parallel;
- `core.activity`, `core.search`, and `core.access` retain their existing shared
  ownership.

Migration 0070 introduces:

- `rooms`: stable project-local slug, type (`project`, `work_item`, `agent`, or
  `brief`), title, purpose, visibility (`project` or `members`), optional issue or
  agent link, creator, timestamps, and archive state;
- `room_events`: one-to-one metadata for an append-only activity row, including
  event kind, optional authoritative-record reference, content digest, and an
  optional superseded event;
- database constraints and triggers that enforce valid type/link combinations,
  one main/brief/work-item/agent room per owning record, immutable room events,
  same-room supersession, and no deletion of referenced audit rows;
- a delivery eligibility bit on activity. Existing rows remain eligible; room
  coordination events are not visible to automation or outbound webhooks.

Generated `main`, `brief`, `work-item-{id}`, and `agent-{id}` slugs are reserved
for their matching records so a custom room cannot poison a later idempotent
ensure operation.

Every room-facing SQLite identifier, including each decoded cursor component,
must be a positive signed 64-bit integer (`1..2^63-1`). Booleans, zero or
negative values, and oversized integers are rejected before they reach SQLite.

The migration backfills a main project room, a live brief, a focused room for
every existing project issue, and project-local rooms for participating agents,
including agents that contributed by creating an issue.
Project, issue, assignment, contributor, and membership commands maintain the
same rooms inside their existing transactions for new or moved records.

## Authorization

All reads first apply `access.can_see_project`. `visibility=project` inherits the
project audience. `visibility=members` additionally requires a signed-in project
creator, admin, or explicit project member. Missing and hidden rooms use one 404
shape, and all room responses are private/no-store and actor-varying.

Room writes require a non-viewer actor and a `rooms:write` bearer scope (browser
sessions are not token-scoped). The command re-resolves the live actor and room
inside `BEGIN IMMEDIATE`; transport checks are never treated as authorization.
Only a human project creator or human admin may create/archive rooms or post a
system notice. Brief rooms are read-only. The request DTO has no actor field, so
client-controlled identity is rejected as an extra field.

Linked issue, page, approval, activity, handoff, dispatch, and run references are
resolved and authorized independently. A reference that is no longer visible is
rendered as unavailable rather than leaking its title, snippet, count, or state.

## Timeline

The timeline is a newest-first keyset page ordered by append-only activity id.
Its opaque cursor binds the room and last visible activity row, cannot be replayed
against another room, and cannot raise the server-side page cap.

- Project and brief rooms project visible activity scoped to their project.
- Work-item rooms project visible activity targeting their issue.
- Agent rooms project the linked agent's visible contributions in that project.
- Every room also includes its own room-authored events.

`room_events` classifies a room-authored activity row as `message`, `check_in`,
`handoff`, `decision`, `evidence`, or `system_notice`. Domain activity remains
authoritative and is classified at read time as human, agent, system, approval,
evidence, or imported. Supersession appends a replacement that points backward;
it never edits or deletes the original. Public history retains both entries and
marks whether each room event is current, plus the visible successor when one is
authorized. Current context and brief projections exclude superseded room events.
A brief room projects current events from every room in its project that the
reader can see; the projection does not weaken a member-only room boundary.


The public timeline is an honest history view: authorized imported activity may
appear there with the explicit `imported` classification. Foreign history never
acts as current Athena state. Briefs, context packets, teammate/contribution
projections, revision receipts, references, and room-event search all exclude
imported rows before counting, ranking, or limiting.


Room text is bounded plain coordination prose. DTO and command validation reject
control characters, direct filesystem paths, credential-like material, and
arbitrary provider/log payloads. The only stored reference is a controlled record
type and identifier.

Already-authoritative source text is projected through the same safety boundary.
Filesystem paths, credential-like material, provider/log payloads, and structured
payloads are replaced with one fixed redaction marker rather than copied or partly
redacted. A context record whose source required redaction reports no source digest:
a digest must not attest to raw text that the packet deliberately withholds.
All externally supplied database identifiers, controlled integer references, and
decoded cursor components must fit SQLite's positive signed 64-bit range before a
query is bound. Opaque cursors remain canonical and room-bound.


## Visible agents

The member panel derives participating agents from project membership, linked
issue ownership/contributors, the room's linked agent, and visible room activity.
For each bounded agent it shows identity, role, pause/revocation state, current
visible claimed work, latest cooperative check-in, recent contributions, and
visible run lineage. Token scopes and credential posture are admin-only; other
readers get an explicit `capability_unavailable` reason rather than an inferred
capability. A lease/check-in is labeled as a recorded claim/report, never proof a
process is alive or executing.

Run receipts, cooperative check-in navigation, and visible lineage are navigable
only when the run is entirely native and every event in it is visible to the
requester. Otherwise Athena suppresses the receipt or lineage item and reports the
explicit `incomplete_or_mixed_visibility_run` reason; a visible contribution's run
identifier may remain only as inert audit metadata.

## `athena.room-context.v1`

`POST /rooms/{room_id}/context` accepts one strict, bounded question and returns:

- schema id, room id/key, requester identity, normalized bounded query, and one
  SQLite snapshot time;
- selected records with exact type/id, title or bounded snippet where authorized,
  source revision/activity id, SHA-256 digest when available, and an internal HTTP
  receipt path rather than a filesystem path;
- visible-candidate and selected bounds, clipping/omission metadata, and explicit
  uncertainty/non-guarantees.

Selection is deterministic and model-free. It intersects existing FTS/query
primitives with the room's visible project/issue/agent scope, then ranks only the
already-authorized candidates with stable tie-breakers. Hidden candidates never
affect snippets, ranks, counts, clipping, or uncertainty text. The packet makes no
claim of truth, completeness, approval, current execution, or causal explanation.
No LLM, provider, network, or filesystem call occurs.

## Live brief

Brief rooms are read-only projections built in one SQLite read transaction from:

- project title and purpose;
- bounded open/high-priority and visibly blocked issues;
- visible active/recent agent claims and cooperative check-ins;
- recent approval/decision facts;
- pages linked to visible project issues; and
- recent visible room/project activity since an optional bounded cursor.

Each group reports its visible count and clipping state. Empty and degraded groups
state what is absent or unavailable instead of manufacturing status.

## Surfaces

REST:

- `GET/POST /projects/{project_id}/rooms`
- `GET /rooms/{room_id}` and `POST /rooms/{room_id}/archive`
- `GET /rooms/{room_id}/timeline`
- `POST /rooms/{room_id}/events`
- `POST /rooms/{room_id}/context`
- `GET /rooms/{room_id}/brief`

Browser:

- project list/detail entry points to `/aegis/projects/{project_id}/rooms`;
- `/aegis/rooms/{room_id}` renders timeline, linked work/knowledge, agent panel,
  live brief when applicable, and the Ask the Room receipt form;
- POST adapters call the same commands as REST and use ordinary CSRF protection.

MCP exposes list/get/timeline/event/context/brief operations through the official
REST client. Messages remain inert on every transport.

## Implementation sequence

1. Land migration, room data access/commands, access rules, internal-only activity
   delivery semantics, and project/issue default-room creation.
2. Land timeline, agent projection, context packet, brief projection, strict REST
   models, and MCP parity.
3. Land Jinja/HTMX routes/templates/styles, project entry points, and demo seed.
4. Prove authorization and cross-project isolation, identity non-spoofing,
   idempotency/concurrency, append-only supersession, visibility-safe search,
   cursor/context bounds, no side effects, packaging, and real HTTP behavior.

## Demo path

Run `athena-demo` against a new database, open the seeded Athena Review project,
choose **Rooms**, then:

1. open the project room and inspect mixed human/agent/system/approval/evidence
   timeline badges;
2. open the seeded issue and agent rooms to inspect linked work and capability
   states;
3. open the brief and verify live priorities, blockers, agents, decisions,
   knowledge, and recent activity;
4. ask “What is blocked and what was decided?” and inspect the model-free packet's
   source receipts, bounds, and uncertainty notice.

## Remaining non-goals

No DMs, invitations, independent room ACLs, rich media, public share links,
reactions, typing/presence, WebSockets, Nostr/federation, Git hosting, workflow
execution, dispatch-on-message, summarizing model, provider egress, or cloud
dependency.
