- **Every refusal carries a machine `code`.** Each 4xx and 5xx JSON body now
  has a top-level `code` from one registry beside its unchanged status and
  `detail`, including the framework's own 404, 405 and 422, and an unhandled
  exception answers a JSON 500. Over MCP a refused call is one JSON object with
  `isError` true, and some codes name the `next` call that resolves them. The
  codes are listed in `docs/AGENT_API.md`.
