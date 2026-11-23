- **One `create_issue` sets up a slice, and every write that changes an issue's
  ETag answers the new one.** `POST /issues` and the `create_issue` tool accept
  `parent_id`, `assignee_id`, `delegate_ids` (agents) and `blocked_by`, applied
  in the create's one transaction through the commands their own routes use:
  each is refused as there, a hidden parent or blocker reads as a missing one,
  and any refusal creates nothing. `PUT /issues/{id}/parent`, archive,
  unarchive and the label writes now send the `ETag` header (`_etag` over MCP);
  `POST /issues/bulk` names each applied issue's `etag`, a playbook start each
  created issue's, and an undo the reversed issue's `target_etag`. The
  conductor recipe sets up an outcome and three slices in 5 requests, where it
  took 21; `tests/test_issue_etag_writes.py` derives the list of such writes
  from the code.
