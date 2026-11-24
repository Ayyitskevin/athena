- **An admin can release another holder's live lease from the issue page, and
  the trail records it as a release.** Reassigning claimed work used to leave
  the old holder's lease in place, so the new agent was refused `claim_held`
  until someone completed the lease over REST. The issue page's Claim card now
  offers an admin "Release lease", confirmed before it is sent, through the
  same `complete_claim` command REST uses. The form carries the generation of
  the possession the page showed, and the command's generation fence refuses
  any other one, so a lease settled and reclaimed since the page was drawn is
  not ended, while a renewal still is.

  Decision for this change, taken by the lead: an admin ending another
  account's lease is a release, with its own verb. That command now records
  `lease_released` (naming the holder and the generation, with the
  `generation` field) instead of a `claim_completed` by the admin, tells the
  holder, and refuses a completion report on it, on REST and in the browser
  alike. No reader of completions counts it: work progress, the project brief,
  the operator's "since you looked", the issue narrative, settlement, review
  requests and "you completed this". The holder's own completion is
  unchanged. A release is not reassignment: a released holder that is still
  the assignee or a contributor may claim the issue again.
