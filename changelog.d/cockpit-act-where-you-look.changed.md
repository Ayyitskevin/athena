- **The cockpit says what an approval is about, reads a completion report
  everywhere, and offers undo on the issue page.** An approval row in the
  "Waiting on you" queue now names the issue's key and title and, for a
  close, the completion the ask rests on: its verdict, who reported it and
  when, the requester's own completion recorded before the ask when there is
  one. A completion recorded after the ask is shown beside it, never in its
  place. All of it is read as far as the viewer could already read it.

  A completion report renders through one partial on the issue page, the
  activity feed and its runs and lineage views, the dashboard, the agents page,
  the inbox, the work-context page and the issue history, instead of raw JSON.
  It is escaped text: long values wrap, fields the partial does not know are
  shown, and an imported completion is shown as its text, never as a native
  report.

  The issue page's History offers Undo for its newest two-way event not yet
  undone, when the viewer could make its inverse now: the effect is still in
  force, the viewer may write the issue, and, when the inverse would close the
  issue, the viewer's approval gate would let the close through. When that
  event cannot be undone the page offers nothing: it never moves down to an
  older event, which is still undone from the activity page. It posts the
  activity page's own form and returns to the issue. When the inverse still needs
  approval, the click asks for it and says so in the page. A durable action
  budget and a project's blocked-close policy are not predicted: the click
  answers for those.
