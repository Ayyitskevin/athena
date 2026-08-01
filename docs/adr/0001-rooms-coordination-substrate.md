# ADR 0001: Rooms are a coordination substrate

- Status: Accepted
- Date: 2026-07-31
- Decision owners: Athena maintainers

## Context

Athena already owns durable work, knowledge, actor identity, approvals, run
history, and visibility envelopes, but the operator must visit separate surfaces
to reconstruct why a piece of work is moving or blocked. Block's public
[Buzz](https://github.com/block/buzz) project and its
[agent vision](https://github.com/block/buzz/blob/main/VISION_AGENT.md),
[project vision](https://github.com/block/buzz/blob/main/VISION_PROJECTS.md), and
[architecture](https://github.com/block/buzz/blob/main/ARCHITECTURE.md)
demonstrate a useful product idea: put people, agents, discussion, work facts,
decisions, and receipts in one durable room. Its implementation is intentionally
a different system: a Nostr relay, signed event protocol, Rust services,
WebSockets, Postgres/Redis/S3, desktop and mobile clients, workflow execution,
and Git hosting.

Athena is a single-process, SQLite-backed command workspace for one operator.
It coordinates and records work; Icarus executes guarded repository work and
Minerva produces provenance-first research. A message or imported record is
neither authority nor proof.

## Decision

Athena Rooms are project-scoped, durable coordination views over Athena's
authoritative records. A room has its own append-only coordination events, but
issue, page, approval, run, dispatch, artifact, and handoff facts remain owned by
their existing tables and commands. The room timeline projects references to
those facts instead of copying mutable snapshots.

Athena adopts these ideas from Buzz:

- humans and authenticated agents appear as visible, distinguishable authors;
- a focused room is the durable record of why work happened;
- conversation, work facts, approvals, outcomes, and evidence share one ordered
  reading surface;
- agent capability and session state are bounded and honest about what cannot be
  observed;
- a project question yields exact source receipts and explicit incompleteness.

Athena intentionally rejects these Buzz choices:

- Nostr, cryptographic identities, relays, federation, and cross-workspace rooms;
- WebSockets, presence, typing indicators, DMs, reactions, or generic chat;
- Rust/Postgres/Redis/S3 services, desktop/mobile clients, or a JS build chain;
- Git hosting, branch automation, workflow execution, shell/file tools, and
  provider calls;
- treating an event, message, digest, import, or agent assertion as approval,
  authorization, execution state, or truth.

Room messages are inert coordination records. Their command may write only the
room event, its internal append-only audit fact, and derived search metadata in
one SQLite transaction. Room-message audit facts are explicitly ineligible for
automation and outbound webhook delivery. No room write dispatches work, calls a
tool/provider, creates or consumes an approval, schedules work, or performs
external I/O.

Room access is not a second permission system. Every room belongs to one project
and is either visible to everyone who can read that project or narrowed to the
project's existing creator/admin/member roster. Linked records are independently
re-authorized at read time; hidden rows are removed before ranking, counting,
snippeting, or limiting.

`athena.room-context.v1` is a deterministic, model-free evidence packet. It is
the only planned input to a future optional cited answerer. That future adapter
must remain read-only and receive a separate consent, egress, credential, and
provider design; it is not part of Rooms v1.

## Consequences

- Rooms advance Athena's Direct, Observe, Intervene, and Trust/Learn loops
  without widening the execution boundary.
- The activity id remains the stable order for projected timeline facts; room
  metadata classifies room-authored rows without duplicating their text.
- Project visibility changes immediately affect room reads. Revocation cannot be
  bypassed by a stale room search index or context receipt.
- There is no real-time delivery claim. Refresh/HTMX reads are sufficient for the
  local operator workspace.
- The v1 surface stays deliberately small: no DMs, invitations, arbitrary room
  ACLs, rich media, public share links, chat affordances, or summarizing model.
