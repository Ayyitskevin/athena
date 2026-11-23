- **A seat can tell, before it claims, whether an issue says what done means.**
  An issue template writes the body as six Markdown headings (goal, context,
  inputs, action, format, success criteria), and the work-context packet, full
  and brief, over REST and MCP, reports `work_order`: the parts the body has and
  the parts it lacks, by a fixed rule over its headings (docs/WORK_CONTEXT.md).
  Nothing is refused for a missing part. The new-issue form can start from the
  template, `create_issue` names it, and the conductor recipe writes issues with it.
