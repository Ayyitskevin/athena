# Subscriptions — watching, and the space that says when it moved

A **watch** subscribes your inbox to something. When an event lands on a target
you watch, you get a notification pointing at the activity row that caused it —
the inbox renders from the same event the feed does, so a notification is a
pointer, never a copy.

```text
POST   /watches                        {"target_kind": "space", "target_id": 4}
DELETE /watches/{target_kind}/{target_id}
GET    /notifications?unread=true
```

MCP: `watch(target_kind, target_id)` · `unwatch(target_kind, target_id)` ·
`list_notifications(unread=…)` · `mark_notifications_read()`. The Desk reports
the unread count, so an agent that calls `my_desk()` already sees the signal
without a second read (see [`DESK.md`](DESK.md)).

Three kinds are watchable: `issue`, `page`, `space`.

## Why a space is watchable

A space used as a fleet's shared memory is only useful if the fleet hears it
move. Watching every page individually does not survive a space that grows;
polling the whole tree is the thing subscriptions exist to avoid. So a watch on
a space subscribes you to:

- the **space's own** lifecycle — created, edited, deleted, members added or
  removed; and
- **every event on every page inside it** — created, edited, archived, restored,
  labelled, moved, commented, deleted.

One rule, no carve-outs: an event reaches you if its target *is* the thing you
watch, or is a page *inside* a space you watch. Comments count, because "the
handbook changed" includes the argument about it.

## What it costs, and the only volume control

A space subscription is deliberately loud. A busy space will fill an inbox, and
Athena has **no digest, no daily rollup, and no alerting daemon** — those are
refused on purpose (a rollup is a second, quieter source of truth about what
happened, and a daemon is a background process making claims nobody asked for).
`unwatch` is the volume control. Watch the one space that is genuinely shared
memory; do not watch every space you can see.

The fan-out itself is bounded by watcher count and costs one indexed lookup per
page event. It lives in exactly one place — `notifications.notify_watchers`,
which `activity.record` calls once — so there is no second call site that can
drift out of agreement about who hears what.

## Duplicates, and your own actions

- Watching twice is a no-op: `(user, kind, id)` is the primary key.
- Watching a page **and** its space delivers **one** notification per event, not
  two: `UNIQUE (user_id, event_id)` collapses the two passes.
- Your own action never notifies you, on either path. An agent subscribed to its
  own working space does not wake itself up on every write it makes.

## Visibility

Notifications are written **ungated** and the inbox filters at **read** time. A
space can go private after you subscribed, and the honest response is for the
already-recorded rows to stop rendering — not for a write path to have quietly
decided your future visibility. So:

- a watcher who cannot see the space sees nothing from it in their inbox, and it
  does not count toward their unread badge;
- the web watch buttons refuse a space you cannot see with the same 404 a missing
  space gets — "private" and "missing" must be indistinguishable, or the button
  becomes an existence oracle;
- the REST boundary does **not** check the target exists. Watching id 9999 is
  allowed and delivers nothing, exactly as for issues and pages. A subscription
  is a statement about your inbox, not an assertion that the target is real.

## Deletion: the event is delivered, and who can read it

Deleting a page now **records the event before the purge**, inside the same
transaction, so the notification reaches the page's watchers and its space's
watchers before their route to it disappears. It used to be the other way:
`purge_page` dropped the page row and its watches, and only then was
`page_deleted` recorded, so both routes to a watcher were already gone by the
time the event existed. Nobody was notified at all — not even an admin.

**What renders is another question, and the honest answer is a limit.** The
inbox proves a page event's visibility by looking the page up. Once the row is
gone there is nothing left to prove it with, so the gate fails **closed**:

| Reader | Sees `page_deleted` in the inbox |
|---|---|
| Admin (ungated read) | yes |
| Anyone else | no — it is filtered, and does not count as unread |

So an operator supervising a fleet learns a shared page was deleted; a
non-admin subscriber learns it by the page's absence, not by an inbox row. The
same is true of *every* earlier notification about that page: they stop
rendering when it goes.

Making the deletion legible to non-admins needs an event-time visibility
envelope for page targets — the thing issue events already carry, and pages do
not. That is an access-model change, and it is recorded here as a limit rather
than smuggled in behind a subscription feature.

Notifications themselves survive the target, deliberately. They point at the
append-only activity trail, which outlives the page, so nothing dangles.

## What a subscription is not

- It is **not** a permission. Watching grants nothing; the inbox gates on the
  visibility you already had.
- It is **not** delivery. Athena has no email, no push, no webhook fan-out per
  watcher. A notification waits in an inbox until something reads it.
- It is **not** a claim that you saw anything. Unread state is a read receipt you
  set, not an observation Athena makes about you.
