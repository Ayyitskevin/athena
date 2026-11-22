- **An agent-facing OpenAPI document at `/openapi-agent.json`.** It lists the
  220 operations an agent's token can call and leaves out the 173 browser pages
  and form posts `/openapi.json` also carries. Each operation declares what the
  code enforces: the bearer scheme where a credential is read, `Idempotency-Key`
  where the idempotency middleware accepts one, `If-Match` and the `ETag`
  response header where the route reads or sets them, the run headers on
  writes, and a shared `Refusal` body (`detail` and a registered `code`) on its
  error answers. It is public metadata under the same access rule as
  `/openapi.json`, which is unchanged.
