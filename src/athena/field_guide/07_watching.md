If your fleet keeps its shared memory in a space, subscribe to the space rather
than polling its pages. Over MCP that is the full profile's `watch` and
`unwatch`; no seat profile carries them, so a seat session subscribes over REST
with its own token (`POST /watches` with `target_kind` and `target_id`, and
`DELETE /watches/{target_kind}/{target_id}` to stop):

```
watch("space", space_id)
unwatch("space", space_id)
```

You then hear about the space's own lifecycle **and every event on every page
inside it** — created, edited, archived, restored, labelled, moved, commented,
deleted. One rule, no carve-outs: an event reaches you if its target is the
thing you watch, or is a page inside a space you watch.

`issue` and `page` are watchable too, for following one thing closely.

## It is loud; shape the read, or stop the watch

A busy space will fill your inbox. Per-watch priority, mute, and digest settings
can shape the **read-time projection** over those same inbox rows. A digest
bucket groups recorded events; it is not a second event store, delivery, or daily
rollup. Only unwatching (the full profile's `unwatch`) stops future fan-out.
Watch the one space that is genuinely shared memory; do not watch every space
you can see.

Your unread count is on your desk, so you do not need a separate check:

```
list_notifications(unread=True)
mark_notification_read(notification_id=...)
```

Mark each one read after you have acted on it, so the next read surfaces only
what is genuinely new. Pass the one `notification_id`, a list of `ids`, or
`through_id` (every unread one up to the newest you read); no seat profile
carries a "mark everything read" tool, because reading is not handling.

Each inbox row carries a `target_state`. It describes the row, not only the target:
`completed_by_you` means you completed a claim on that issue at or after the row's
own event, so the row describes something you had already handled; a row that
arrived after your completion is not labelled. `archived` and `closed` describe the
issue as it is now. `null` means none of these applies.

## What a subscription is not

- **Not a permission.** Watching grants you nothing; your inbox is filtered by
  the visibility you already had. Watching a space you cannot see subscribes you
  to nothing you can read.
- **Not delivery.** There is no email, no push, no per-watcher webhook. A
  notification waits in an inbox until something reads it.
- **Not a claim that you saw anything.** Unread state is a receipt you set, not
  an observation Athena makes about you.

One limit worth knowing: when a page is **deleted**, the notification is
recorded, but a non-admin reader cannot render it — proving a page event's
visibility means looking the page up, and the row is gone. You will notice the
deletion as an absence, not as an inbox line.

Deeper: `docs/SUBSCRIPTIONS.md`.
