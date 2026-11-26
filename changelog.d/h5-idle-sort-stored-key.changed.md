- **The idle sort reads a stored key through an index (H5).** `sort:idle-desc`
  computed each matching issue's last work time from its trail and sorted them
  all before taking a page: 8.0 M SQLite steps at 50,000 issues. Migration 0096
  stores the last work time (`issues.last_work_at`) and the open/closed verdict
  (`issues.status_closed`) on each issue, keeps both with triggers in the same
  statement as the event or status change that moves them, backfills them from
  the trail, and indexes them. A page of `sort:idle-desc`, with or without
  `is:open`, `is:closed` or `idle:`, now takes about 4,700 steps at 1,000,
  10,000 and 50,000 issues alike. The definition of idle and of closed is
  unchanged, and every idle sort and filter answers the same issues in the same
  order; a property test replays random histories against the old computation.
  An issue read does not carry the two columns.
