- **`athena-mcp` serves the boot block, the loop guide and the glossary as MCP
  resources (A14).** An agent that reaches Athena only through MCP reads them at
  `athena://docs/boot-block`, `athena://docs/loop-guide` and
  `athena://docs/glossary`, in every profile. They ship in the wheel as package
  data (`src/athena/mcp/documents/`), byte-identical copies of `docs/` that a test
  and the wheel gate hold. The server no longer advertises a prompts capability,
  since it serves no prompt.

  The glossary now has a row for every word in an MCP tool name apart from a short
  list of plain verbs and connectives, including desk, office, floor, inbox, watch,
  yield and claim-next, and every row says what the word is not the same as.
  `tests/test_glossary_tool_words.py` fails when a tool name brings a word the
  glossary does not define. No tool, tool description or server instruction
  changed.
