- **The issue list, a space's page list and an issue's comment list page with
  a cursor, and one comment can be read whole.** `GET /issues`,
  `GET /spaces/{id}/pages` and `GET /issues/{id}/comments` now answer
  `{issues|pages|comments, next_cursor, has_more}` (decision D2) instead of a bare
  list: at most `limit` rows a call (issues 50 by default and 100 at most, pages
  100 and 200, comments 50 and 200). While `has_more` is true, send `next_cursor`
  back as `cursor`; a row added between two calls never repeats or hides another.
  The page list's rows no longer carry the body (`GET /pages/{id}` does), so 300
  pages of 2,100 characters list in 3 calls of about 23 kB where one call sent
  702 kB. The comment list was unbounded. `GET /issues` no longer takes `offset`:
  a caller still sending it is refused with 422 rather than served the first page.
  `GET /issues/{issue_id}/comments/{comment_id}` and the MCP tool
  `get_issue_comment` (in `seat`, so in every seat profile: 28, 39 and 41 tools)
  read one comment whole; a comment on an issue the caller cannot see is 404. The
  MCP tools `list_pages`, `list_issue_comments`, `search_work` and `list_issues`
  take `cursor` where they took `offset` and continue with the server's cursor,
  so listing 300 pages through `list_pages` fetches each page once (6 calls,
  70 kB from REST) where it fetched the whole list again for every call (5
  calls, 3.5 MB). A comment row cut by `max_chars` names `get_issue_comment` to read on.
