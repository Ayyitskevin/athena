- **The desk and the work-context packet stop repeating their explanations
  (AD2, D29).** Over MCP, `my_desk` and `get_issue_work_context` (full and
  brief) now answer lean by default: without each lane's `meaning`, the work
  protocol (it was there twice), the office's and the packet's `semantics`
  (with `readiness`), the packet's `protocol` and the fixed half of its
  `how_to_claim`, and without any key whose value is null. A lean answer names
  its own schema: `athena.agent_desk.v3` for the desk and
  `athena.issue_work_context.v2` for the packet; nothing else differs, and a key
  absent from it is null. At the demo seed the desk an agent reads first drops
  from 7,485 to 3,254 bytes and the work context of the issue it holds from
  7,155 to 4,733. The texts are served once, by schema and key path, in the new
  answer glossary: the MCP resource `athena://answers/glossary`, `GET
  /agent/glossary` (any token) and docs/ANSWER_GLOSSARY.md, generated from the
  constants the answers use. `explain=true` on either tool returns the full
  answer exactly as before. REST is unchanged by default: `GET /desk` and
  `GET /issues/{ref}/work-context` answer the full shape unless you send
  `?lean=true`, which returns the lean one with the same `ETag`. An MCP client
  that read a null-valued key by indexing it (`row["read_at"]`) must now treat
  a missing key as null, or pass `explain=true`.
