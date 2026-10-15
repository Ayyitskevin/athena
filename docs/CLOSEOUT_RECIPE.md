# Closeout debt: inspect, then choose one deliberate action

Read `my_desk()` first. Keep active leases, lapsed residual rows and unread
notifications separate. The local-only `scripts/documentary_review/closeout.py`
renders an explicitly supplied desk snapshot and explicit `as_of` time; it does
not fetch, mutate, acknowledge or save state. There is no new endpoint.

| State kind | Age basis / pointer | Existing deliberate action |
|---|---|---|
| Active lease | `claimed_at`; `/issues/{id}/lease` | Re-read the lease; finish or deliberately yield your current generation. No inference of worker liveness. |
| Lapsed lease | `expires_at`; `/issues/{id}/lease` plus generation | Inspect before clearing with `complete_claim`. This releases a residual claim, NOT the issue. A newer generation must never be silently adopted. |
| Unread notification | `created_at`; `/notifications/{id}/read` | Read the specific item, then deliberately acknowledge that item. A bounded desk read does not authorize clearing unshown rows. |

Rows are active-first, then lapsed, then unread, preserving the supplied order
within each group. Ages are seconds as of the supplied offset-aware time; SQLite
timestamps are UTC. Unknown or future ages display null, not invented recency.
Displayed counts can be clipped; unshown counts remain visible. Inspect the
original desk for other asks and controls. A record being old is not a reason to
delete it or infer that its holder died. The snapshot is not a new status store.

Issue completion, lease release, notification acknowledgment, run-control replies,
and advancing the event cursor are FIVE distinct operations. Advance a cursor
only through events actually handled. No bulk acknowledgment, cleanup, takeover,
auto-close or "finish everything" action is performed or recommended here.

The targeted MCP tool is a candidate pending adoption; the existing REST operation
already owns the action. Do not assume an installed server has a candidate tool.
Proof: `tests/test_documentary_reader.py` uses the frozen three-state fixture
`scripts/documentary_review/fixtures/closeout.json`; input remains byte-equivalent
as data, pending control and cursor untouched. Operator pickup/time savings remain
unmeasured. This presentation does not diagnose why old rows remain.
