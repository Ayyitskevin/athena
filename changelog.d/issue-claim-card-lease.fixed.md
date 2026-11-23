- **The issue page's Claim card shows the lease, not the assignee.** It read
  "unclaimed" while an agent held a live lease, because it rendered the
  assignee. The card now names the lease holder, when the lease expires and
  when it was last claimed or renewed; an expired lease nobody released reads
  "unclaimed" with a "lapsed lease" chip and its last holder; with no lease it
  says so. The assignee has a cell of its own, labelled as the accountable
  owner. The card reads the same lease row as `GET /issues/{id}/lease`, under
  the same visibility. Boards and the issue list still group by assignee.
