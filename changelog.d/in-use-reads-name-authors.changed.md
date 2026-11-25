- **Issue lists, searches and bulk rows say what an agent needs (G10).**
  `GET /issues`, `GET /issues/{ref}`, `GET /issues/{id}/children`,
  `GET /issues/search` and `GET /search` carry `authored_by` on each row or
  hit: who wrote the current text (for a comment hit, the comment's author),
  `null` when Athena does not know. Write answers do not carry it, so an
  issue's ETag is unchanged. A row of `POST /issues/bulk` refused for a status
  the project does not offer lists `valid_statuses`, as the single write does. The author lookup
  is batched: one query for a page of issues, not three per issue.
