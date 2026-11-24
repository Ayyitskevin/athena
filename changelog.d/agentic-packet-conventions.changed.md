- **Agent packets speak one dialect of time, lanes and paging (A13).** Every
  timestamp in the desk (and `/office`, `/desk/ready`, `/desk/since`), the work
  context, the lease and claim answers, the event feed, the notification,
  run-control and delegation inboxes, and the conductor's project brief, floor,
  review queue, issue history and attention ranking is RFC 3339 UTC to the second
  (`2026-11-21T18:19:13Z`); one desk read used to carry three shapes. Each
  `<x>expires_at` has `<x>expires_in_seconds` beside it, counted from the packet's
  own clock read (the same read that decides `active`), and `/openapi-agent.json`
  declares those timestamps `format: date-time`. The desk's `asks.review_requests`
  is present and empty when nothing is asked, like every other lane. Paging styles
  are frozen by a test: existing lists keep theirs, and a new one pages with
  `cursor` in, `next_cursor` and `has_more` out (D2); the eligible-accounts read
  added in this release is the first to use it. Storage, issue ETags and cursors
  are unchanged; the issue, comment, page, label, project and search reads keep
  their own shapes, and the browser its own display. A work-context ETag held
  across the upgrade no longer matches, because the tag covers the packet as it is
  sent; read the context again once. A client that compared a packet timestamp as
  a string against a stored one must compare instants; `since` on the admin
  security reads accepts the packet shape.
