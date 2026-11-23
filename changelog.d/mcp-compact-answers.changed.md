- **MCP answers are sent as compact JSON.** The text of every tool answer, and
  of every failure, is the same object without indentation or spaces between
  tokens; the structured content and the output schemas are unchanged. The
  budget is still 16,000 characters (`get_issue_work_context` 32,000) of the
  text as sent, so an answer the budget cuts carries more: indentation was 16%
  of what the three seat profiles' tools sent on the budget test's workspace,
  and up to a third of a structured answer such as `list_notifications`.
  `FastMCP.call_tool` called in-process still returns the SDK's indented text.
