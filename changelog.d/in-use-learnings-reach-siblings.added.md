- **A learning reaches the parent's other slices (W5, decision D22).** The
  work-context packet carries `learnings`: the newest learnings recorded on
  the issue, on its parent and on its siblings under the same parent, newest
  first, at most five, each with `issue_id`, `relation`, `runbook_page_id`,
  `recorded_at`, `authored_by`, an `excerpt` of at most 500 characters with
  `truncated`, and `advisory_untrusted`. Visibility applies to every issue
  and runbook page read. A runbook edited by hand so that its text can no
  longer be paired with its recorders gives `excerpt: null`, not a wrong
  author. No new table, and no scope wider than the parent.
