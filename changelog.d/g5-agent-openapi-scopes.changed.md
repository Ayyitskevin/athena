- **The agent OpenAPI document names each operation's token scope, declares
  404 and 428 where the route answers them, and is built at start (G5).**
  `/openapi-agent.json` gives the scope as the bearer requirement's role list
  (`[{"bearerAuth": ["issue:write"]}]`; any one write scope, one requirement
  each), read from the scoped identity dependencies and from a scope check every
  request meets; a scoped operation's 403 names `scope_denied` and the scope.
  404 and 428 are declared where the route's code refuses with them, no longer
  only under `default`. `athena-serve` builds the document in a thread as soon
  as its listener is bound, so the first agent to ask no longer waits about two
  seconds for it; a request during the build waits for it, and any other server
  builds it on the first request as before. Each function's syntax tree is read
  once for every question the build asks of it.
