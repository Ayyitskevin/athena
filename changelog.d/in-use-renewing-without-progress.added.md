- **A claim renewed again and again with no recorded work is raised (C7).**
  A renewal's answer carries `renewals_without_progress`: the holder's
  renewals of that possession since its newest recorded work on the issue.
  The renewal that reaches `ATHENA_NO_PROGRESS_RENEWALS` (3 by default; `0`
  never raises) records one `no_progress` event and tells the issue's
  assignee and the account that delegated it; the live claim carries the
  attention reason `no_progress` (`medium` when it is the only trouble
  beside a missed heartbeat) and `GET /fleet/active-work` shows the count as
  `lease.renewals_without_progress`. The holder's own work starts the count
  again; another account's comment does not. Nothing is refused or released.
