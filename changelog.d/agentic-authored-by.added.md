- **Peer-written text names its author.** Objects that hold free text in the
  work-context packet, the desk and the comment, page and notification reads
  carry `authored_by: {id, is_agent}` for the account that wrote the current
  text, or null when Athena does not know. The MCP server instructions state
  that such text is data, never instructions to the reader.
