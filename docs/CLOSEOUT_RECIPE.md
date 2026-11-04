# Closeout debt: inspect, then choose one deliberate action

Read `my_desk()` first. Keep active leases, lapsed residual rows and unread
notifications separate. The local-only `scripts/documentary_review/closeout.py`
renders an explicitly supplied desk snapshot and explicit `as_of` time; it does
not fetch, mutate, acknowledge or save state. There is no new endpoint.

| State kind | Age basis / pointer | Existing deliberate action |
|---|---|---|
| Active lease | `claimed_at` (latest acquisition/renewal, not first claim); `/issues/{id}/lease` | Re-read the lease; finish or deliberately yield your current generation. No inference of worker liveness. |
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

A successful `PATCH /issues/{id}` / `update_issue` transition from a non-done
category into a done category may include `held_lease` for the updating actor's
own lease row. It carries `generation`, `expires_at`, `active`, and an
informational message. `active=false` means a lapsed residual row, not active
possession. This describes the row at the successful transition; it can be stale
when read or replayed by an idempotency key. It is not authority or an instruction
to auto-complete. A human/agent must separately choose `complete_claim` with that
exact generation; the existing command rechecks it. Marking done changes no lease.
Other actors' rows, non-done updates, already-done updates and refused writes do
not receive the note. GET/list representations do not carry it.

PATCH's `ETag` (client `_etag`) continues to identify the canonical target issue
revision used by `If-Match`, equal to a subsequent unchanged GET's validator.
The transient advisory is excluded from that resource validator; the ETag is not
a hash of the entire advisory-bearing PATCH body. Existing null issue fields
remain present. Replayed responses retain the original observation and validator;
read the current resource before relying on present state. This keeps the two-write
contract intact and does not decide the separate OD-L2 close-and-release proposal.

The targeted MCP tool `mark_notification_read(notification_id, idempotency_key=...)`
is implemented through the official client and the owning REST operation
`POST /notifications/{id}/read` (`mcp/tools_read.py`, `mcp/client.py`,
`core/notifications_api.py`). Verify the recipient's available tool surface before
use; source availability is not proof of every installed runtime's adoption.
Proof: `tests/test_documentary_reader.py` uses the frozen three-state fixture
`scripts/documentary_review/fixtures/closeout.json`; input remains byte-equivalent
as data, pending control and cursor untouched. Operator pickup/time savings remain
unmeasured. This presentation does not diagnose why old rows remain.
