- **The navigation badge and the issue page refresh themselves (U12).** The
  "needs you" badge and the unread count beside it re-ask the server every
  `ATHENA_LIVE_REFRESH_SECONDS` on every page, not only on the dashboard, so a
  question or an approval that starts waiting while the operator reads an
  issue or a Mentor page shows without a reload. An issue page's supervision
  block (who holds the work, under which run, when it last moved, and the
  admin's release control) refreshes the same way and says when it was read.
  Both pass the gate their page applies; a reader with no session polls
  nothing, and a signed-out or paused tab stops polling. Every signed-in tab
  now polls its badge, so the setting's load bound counts tabs, not admins.
