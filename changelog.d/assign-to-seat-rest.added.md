- **A conductor agent can assign an issue to a declared fleet seat.**
  `POST /issues/{issue_id}/assign-seat` and the conductor profile's
  `assign_issue_to_seat` tool hand an issue to a seat by its roster slug, as the
  cockpit's "Assign to a seat" does: the seat's agent account is delegated the
  issue and made its assignee in one write, then the seat is radioed over Buzz.
  It needs a write role and `issue:write` (an admin token included), then the
  issue's own write gate. An issue the caller cannot see answers 404 like a
  missing one, a seat whose agent cannot see the issue's project is refused as
  an unknown account is, and a radio that fails never undoes the assignment.
