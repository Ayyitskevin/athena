- **An issue edit keeps the title and body it replaced, undo restores them, and a
  bearer-token edit of either needs `If-Match` (F10, D20).** This is a
  behaviour change that can refuse a caller that works today: a bearer-token
  `PATCH /issues/{id}` (MCP `update_issue`) or `POST /issues/{id}/link-mention`
  that sends a `title` or `body` without `If-Match` is now refused 428
  `precondition_required`, whose `next` names `get_issue`; with a stale tag it
  is refused 412 `precondition_failed`, as before, and `*` or a list of tags is
  refused 400 `invalid_if_match`. Read the issue and send its current ETag. The
  browser's edit form, the trusted actor header and automation rules are
  unaffected, and a bearer caller changing only status, priority, assignee,
  project, sprint or labels needs no tag. Every edit that
  changes the text stores the text it replaced in the new `issue_prior_texts`
  table, in the edit's transaction and keyed to its `issue_edited` event (an
  edit that changes nothing stores nothing; nothing is ever removed, so each
  edit grows the database by the replaced text plus a 64-character digest per
  changed field). The issue history (`GET /issues/{id}/history`,
  `get_issue_history`) lists each edit as an `edit` item with who, when and
  which fields, and a pointer to the new read `GET
  /issues/{id}/prior-texts/{event_id}` (MCP `get_issue_prior_text`, conductor
  profile), which returns the old text exactly to a caller who can see the
  issue; a hidden issue reads as missing. No other answer carries the old text.
  `issue_edited` is now undoable: undo restores the old text byte for byte,
  is itself undoable, and refuses with `undo_no_effect` once a later edit has
  replaced that text. `update_issue` and `link_mention` are no longer marked
  destructive. Imported issues and imported history carry no prior text, and
  an export carries only an issue's current text. An edit needs a fresh tag,
  not a lease: a seat whose lease lapsed can still edit after reading the issue.
