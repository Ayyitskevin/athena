- **Sign-in lands on the attention queue, and "needs you" is one number.** A
  sign-in (password or SSO, or the sign-in page while signed in) now lands on
  `/aegis/dashboard`, whose first panel is the fleet-attention card, above the
  work totals, instead of the issue list. The sign-in takes no redirect target,
  so nothing a request carries moves it (an owner's decision for this change).
  The Intervene badge shows the fleet-attention total and nothing else: it read
  that total plus unread inbox rows, so it disagreed with the dashboard card (45
  against 11 on the reviewer's workspace). Both read
  `fleet_attention.needs_you`: a page renders the badge and the card from one
  read, and the card's ten-second refresh now carries the badge with it, so a
  refreshed card no longer sits beside the page load's badge. The unread inbox
  is shown beside the badge as its own labelled count (on a narrow screen only
  in the Intervene menu's Inbox link). The rollup is now read only when a page
  renders the nav or the card refreshes, once per request: an admin's JSON
  requests, health checks and 404s no longer build it, and the dashboard no
  longer builds it twice.
