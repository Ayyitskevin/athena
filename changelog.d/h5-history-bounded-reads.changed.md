- **The project brief and a search for a common word no longer read the whole
  history (H5).** `GET /projects/{id}/brief` found each issue's latest
  completion by grouping every issue event in the workspace; it now asks each
  of the project's issues for its own newest visible completion, one index seek
  each, and answers the same. On the seeded workspaces the brief takes about a
  third of the SQLite work it took (2.1 million virtual-machine steps to 0.63
  million at 10,000 issues, 10.6 million to 3.1 million at 50,000); it still
  reads every issue of its project, so it still grows with the project.
  Full-text search (`GET /search`, `GET /issues/search`, plain words in
  `GET /search/workspace` and `/find`) scored every entry containing the word
  and listed every visible and archived issue and comment on each call; it now
  ranks the newest 1,000 matching entries the caller may see
  (`search.RANK_WINDOW`) and checks each hit's own row. A word in fewer entries
  ranks exactly as before; for a commoner word the best hits come from its
  newest 1,000 entries, and paging stops there. A search for a word in every
  issue took 67,000, 659,000 and 3.3 million steps at 1,000, 10,000 and 50,000
  issues; it now takes about 78,000 at each. The idle sort (`sort:idle-desc`)
  is unchanged and still reads every matching issue; `docs/PERFORMANCE.md` says
  what bounding it would take.
