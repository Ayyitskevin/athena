- **An agent is woken only by events it asked about, and reads facts as fields
  (F8).** `GET /events` and the MCP `wait_for_events` and `recent_events` take
  `project_id` (the project's own events and those of the issues that were in it)
  and `mine=true` (the issues you are assigned, delegated or created, and those
  you delegated to another account); a filtered
  wait answers and wakes only on a matching event, and a project you cannot see
  reads exactly as one that does not exist. Each filter seeks an index (migration
  0086 adds the three `mine` needs). A lease event carries its `generation`, a yield
  or handoff resume its `handoff_token` and a completion its reported `verdict` as
  fields, recorded beside the unchanged prose `detail`; an event with none of
  them reads as before.
