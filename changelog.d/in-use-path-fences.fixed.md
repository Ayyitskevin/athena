- **Path fences work without a root catalog.** On a default instance a second
  lease declaring any path at all was refused `409 conflict`, whatever the path.
  Paths are now compared under one implicit root, segment by segment, across
  projects (`path_fence` and `office.fence` say `lexical, root unknown`):
  disjoint fences coexist. An overlap is refused `fence_overlap`, and a fence
  that cannot be placed `fence_unresolved`; each carries a `fence` that names
  the caller's path, and the other issue, holder and path only when the caller
  can read that lease. `claim_next_ready` accepts `paths`. The office and desk
  report the comparison basis (`office.fence.basis`) and nothing of the catalog
  itself; a `coordination_root` still comes from the operator, and its input
  schema is now closed.
