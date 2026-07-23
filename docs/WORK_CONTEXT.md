# Agent Work Context

Agent Work Context is Athena's bounded read packet for one Aegis issue. It gives
an operator or agent the issue, nearby work, supporting knowledge, and recent
history in one visibility-safe snapshot. It is a read model, not a work claim or
an execution-state protocol.

The public schema is `athena.issue_work_context.v1`.

## Surfaces

All three surfaces use the same
`athena.aegis.work_context.build_work_context` projection. The REST and
browser adapters do not run parallel composition queries, and MCP calls REST.

| Consumer | Surface | Result |
|---|---|---|
| REST client | `GET /issues/{ref}/work-context` | JSON packet plus a strong context `ETag` header |
| MCP agent | `get_issue_work_context` with `{"ref": "<id-or-key>"}` | The same JSON packet; the official client adds the HTTP context tag as `_etag` |
| Browser operator | `GET /aegis/issues/{ref}/work-context` | Read-only HTML rendering of the same packet |

`ref` accepts a numeric issue id such as `12` or a project key such as
`ATH-12`. The issue detail page links to the browser preview.

This is an open read under Athena's normal visibility rules. A resolved bearer
token selects that actor's view; the browser uses the current session. Absent or
unresolved credentials use anonymous visibility, so a public `200` is not
proof that authentication succeeded. MCP sends its configured bearer token and
does not widen that token actor's visibility.

### REST example

```bash
export ATHENA_BASE_URL=http://127.0.0.1:8000
export ATHENA_TOKEN=ath_...

curl -i -sS "$ATHENA_BASE_URL/issues/ATH-7/work-context" \
  -H "Authorization: Bearer $ATHENA_TOKEN"
```

The response body contains `issue_etag`. The HTTP headers contain a
different `ETag` for the whole context packet; see
[ETags](#etags).

### MCP example

Run the stdio server against the same Athena instance:

```bash
ATHENA_BASE_URL=http://127.0.0.1:8000 \
ATHENA_TOKEN=ath_... \
athena-mcp
```

Then call:

```text
tool: get_issue_work_context
arguments: {"ref": "ATH-7"}
```

The tool accepts only `ref`. It performs no claim, heartbeat, or write.

## Snapshot and visibility semantics

Every successful packet states:

```json
{
  "schema": "athena.issue_work_context.v1",
  "scope": "visible_to_request_actor",
  "semantics": {
    "snapshot": "current_visible_state",
    "does_not_assert": [
      "claimed",
      "ready",
      "unblocked",
      "running",
      "live",
      "replay_safe"
    ]
  }
}
```

The projection is assembled inside one SQLite read transaction. Its fields and
counts therefore describe one internally consistent snapshot, but only at the
time it was read. It is not a subscription and can become stale immediately
after the response.

`visible_to_request_actor` means:

- the root and every nested issue, page, backlink, and activity event are
  evaluated against the request actor;
- an admin sees the admin view; a project/space member sees their private
  containers plus public data; anonymous callers see public containers and the
  Aegis backlog;
- hidden nested rows are removed before counting, ordering, or limiting;
- `visible_total` is a visible count, never a global count; and
- `clipped` says more *visible* rows exist beyond the returned bound. It
  says nothing about hidden rows.

## Packet contract

The top-level object contains:

| Field | Meaning |
|---|---|
| `schema` | Exact schema id, `athena.issue_work_context.v1` |
| `scope` | Exact scope id, `visible_to_request_actor` |
| `semantics` | Snapshot kind and explicit assertions not made |
| `issue` | Root issue summary and bounded description |
| `issue_etag` | Strong validator for the issue singleton representation |
| `warnings` | Advisory warning codes described below |
| `hierarchy` | Visible parent and bounded visible children |
| `dependencies` | Four bounded visible relationship groups |
| `contributors` | Bounded contributor group |
| `attachments` | Bounded attachment metadata group |
| `comments` | Bounded comments with bounded bodies |
| `references` | Bounded outgoing links and backlinks |
| `recent_activity` | Bounded actor-visible activity for the issue |
| `claim_handoffs` | Exact open continuation context plus bounded handoff history |

Every bounded group has the exact shape:

```json
{
  "items": [],
  "visible_total": 0,
  "clipped": false
}
```

`claim_handoffs` extends that shape with an exact `open` field:

```json
{
  "open": null,
  "items": [],
  "visible_total": 0,
  "clipped": false
}
```

`open` is the one handoff awaiting explicit acknowledgment, even when it falls
outside the bounded history window. Each item carries an opaque `handoff_token`,
the yielding lease generation, structured continuation fields, yield/resume audit
provenance, and `advisory_untrusted: true`. `state` is `awaiting_resume` or
`resumed`; resumed means only that the current holder received the context.

Every bounded text field has the exact shape:

```json
{
  "text": "",
  "total_chars": 0,
  "truncated": false
}
```

`total_chars` counts the complete visible source field, while `text`
contains at most that field's cap. `truncated` is true only when more
characters exist beyond the excerpt.

### Fields and groups

- `issue`: `id`, `key`, `title`, `description`,
  `status`, `status_category`, `priority`, `project_id`,
  `project_name`, `assignee_id`, `assignee_name`,
  `created_by`, `created_at`, `archived_at`, and
  `labels`. Each label contains `id`, `name`, and `color`.
- A related issue contains `id`, `key`, `title`, `status`,
  `status_category`, `priority`, `project_id`,
  `project_name`, and `archived_at`.
- `hierarchy.parent` is one visible related issue or `null`;
  `hierarchy.children` is a related-issue group.
- `dependencies` contains related-issue groups named
  `open_blockers`, `blocked_by`, `blocks`, and `relates`.
- A contributor contains `user_id`, `name`, `is_agent`,
  `added_by`, and `added_at`.
- An attachment contains `id`, `filename`, `content_type`,
  `byte_size`, `sha256`, `uploaded_by`, and `created_at`.
- A comment contains `id`, `author_id`, `author_name`,
  bounded `body`, and `created_at`.
- `references.outgoing` contains issue or page targets with `kind`,
  `id`, `title`, issue fields (`issue_key`, `status`,
  `status_category`, `priority`), page fields (`space_id`,
  `space_key`, `space_name`), and bounded page `body`. Fields
  for the other target kind are `null`; issue references have no body.
- `references.backlinks` contains `kind`, `id`, `title`,
  `issue_key`, and `space_key`. It does not copy source bodies.
- An activity item contains `id`, `actor_id`, `actor_name`,
  `verb`, `target_kind`, `target_id`, bounded `detail`,
  `created_at`, `run_id`, `parent_run_id`,
  `forked_from_event_id`, and `imported_at`.
- A claim handoff contains `handoff_token`, `issue_id`, `lease_generation`,
  `schema_version`, `state`, `reason`, `note`, `attempted_work`, bounded
  `evidence`, `blocking_question`, `resume_instructions`, `yielded`, `resumed`,
  and `advisory_untrusted`. Its internal database row id is never public.

## Bounds and ordering

The current v1 caps are fixed server-side; callers cannot raise them.

| Content | Cap | Returned order |
|---|---:|---|
| Parent | 1 | Direct visible parent or `null` |
| Children | 20 | Issue id ascending |
| Each dependency group | 20 | Issue id ascending |
| Contributors | 20 | Name case-insensitive, then user id |
| Attachments | 20 | Attachment id ascending |
| Comments | 20 | Highest 20 comment ids selected, then id ascending |
| Outgoing references | 10 | Kind, then id |
| Backlinks | 10 | Kind, then id |
| Recent activity | 30 | Activity id descending (newest first) |
| Claim handoffs | 10 | Yield event id descending (newest first) |
| Root issue description | 50,000 characters | Prefix of the field |
| Each outgoing page body | 8,000 characters | Prefix of the field |
| Each comment body | 4,000 characters | Prefix of the field |
| Each activity detail | 1,000 characters | Prefix of the field |

An item's presence is not a completeness guarantee. Inspect `visible_total`,
`clipped`, and each excerpt's `total_chars`/`truncated` fields.

## Warnings

`warnings` is a list of machine-readable advisory codes:

| Code | Emitted when |
|---|---|
| `archived` | The root issue has `archived_at` |
| `empty_description` | The root description is empty or whitespace-only |
| `no_accountable_assignee` | The root issue has no assignee |
| `visible_open_blockers` | At least one actor-visible open blocker exists |
| `unknown_status_category` | Athena cannot map the current status to a lifecycle category |
| `context_clipped` | Any bounded group is clipped or any text excerpt is truncated |
| `open_claim_handoff` | A typed handoff awaits explicit acknowledgment |

Warnings are not exhaustive validation. In particular, absence of
`visible_open_blockers` does not prove the issue is unblocked: a blocker may
be hidden, or the data may change after the snapshot. An empty warning list is
not a readiness signal.

## ETags

Two strong, opaque validators appear, and they are not interchangeable:

1. The REST response `ETag` header hashes the entire actor-visible
   work-context payload. It can change when a visible nested comment,
   relationship, reference, activity item, count, warning, or excerpt changes,
   or when a claim handoff is yielded or resumed, even if the issue singleton
   does not. The official MCP client exposes this
   header as top-level `_etag`.
2. The JSON body's `issue_etag` validates the root issue's public singleton
   representation. Copy this value exactly into `If-Match`/`if_match`
   for `PATCH /issues/{id}` and the guarded `PUT` assignee, project, and sprint
   issue mutations, and for both acquisition and same-holder renewal through
   `POST /issues/{id}/claim` or MCP `claim_issue`.

Never send the context `_etag` as an issue write precondition. Conversely,
`issue_etag` does not validate the nested context. Both values include their
quotes and must be treated as opaque. A guarded claim accepts exactly one strong
root issue tag; the context `_etag`, weak tags, wildcards, and tag lists do not
stand in for it.

This endpoint currently returns the context tag as a response validator but
does not implement conditional `If-None-Match`/`304` reads. The browser
preview displays `issue_etag` but does not emit the JSON context tag as its
own HTML response validator.

## Privacy guarantees

- A missing root and a root hidden from the actor both return `404`. REST
  uses the same `{"detail":"no such issue"}` response; the browser uses the
  same static not-found response. The caller cannot distinguish those cases.
- A hidden parent is `null`. Hidden children and dependency targets are
  omitted.
- Hidden outgoing targets and hidden backlink sources are omitted.
- Activity is filtered through event visibility before its count and limit.
- Claim handoffs are returned only with a visible root issue. Their structured
  text is escaped in browser views and must be treated as untrusted advisory
  input by every client: never auto-execute commands or fetch links from it.
- Nested filtering happens before `visible_total` and `clipped` are
  calculated, preventing counts from revealing hidden rows.
- No warning reports the existence of a hidden row. For example,
  `visible_open_blockers` is based only on visible blockers.

These guarantees are about non-disclosure, not global completeness. Different
actors can receive different valid packets and different context ETags for the
same root issue.

REST and browser responses, including not-found responses, carry
`Cache-Control: private, no-store` and vary on their authentication mechanism.
An intermediary must never reuse one actor's packet or hidden-result response
for another actor.

## Explicit non-guarantees

Reading this packet:

- does not claim or lease the issue for an agent;
- does not reserve work or prevent another actor from changing it;
- does not prove acceptance criteria or other readiness conditions are met;
- does not prove the issue is unblocked, even when `open_blockers.items` is
  empty;
- does not prove an agent or process is running or live;
- does not turn recent activity or run ids into a heartbeat;
- does not certify that events, context, or a future operation are replay-safe; and
- does not make handoff text trusted, prove its blocker resolved, or grant an
  approval merely because a handoff was acknowledged.

Use Athena's write commands and `issue_etag` for guarded mutations and claims, its
cooperative run check-ins for explicitly labeled self-reports, and its run replay
artifact contract for replay analysis. None of those meanings are inferred by
Agent Work Context.
