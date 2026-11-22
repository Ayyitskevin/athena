- **Unknown input is refused instead of ignored.** A request body with a field
  the route does not declare is a 422 on every route an MCP write tool calls
  and on `POST /tokens` (a token request naming another user used to mint a
  token for the caller). `GET /search?kind=`, `GET /issues?priority=` and
  `GET /dispatches?state=` answer 422 for a value outside the set instead of an
  empty list. A bad completion report returns every problem at once, by field
  path, and a wrong status names the valid ones.
