- **An issue's assignment, creator or contributors changing fences that issue's
  idempotency receipts only, five narrowings that moved no counter now fence,
  and a fenced key is refused only until its receipt would have expired (F6,
  decision D17).** One workspace-wide authorization revision used to move on any
  change to any issue's assignee, project or creator, so every stored receipt
  answered 409 `idempotency_authorization_changed` after one unrelated
  assignment, and its marker was kept forever (PERF-1). Migration 0090 makes an
  issue's assignee or creator changing, or one of its contributors removed (a
  `DELETE /contributors` or a decline), fence the receipts of keyed writes whose
  path names that issue (a reviewed list in `core/idempotency.py`, which also
  holds the argument: nothing that decides visibility reads them, and only that
  issue's own write gates do), plus every receipt that cannot be scoped (bulk,
  claim-next, every route outside `/issues`); a create is fenced by none of
  these. Role, token, membership, visibility, pause and placement changes still
  fence every receipt, and so now do an account's `is_agent` flag, a project's
  blocked-close policy, an approval gate added or removed, and an action budget
  set or its limit changed, none of which moved a counter before. A keyed
  decline, like a keyed assignment, answers once and then refuses. Spending a
  budget moves nothing: a replay is not metered. The original counter still
  moves on every act, so a process running older code fails closed. A fenced
  marker now expires with its receipt, after which the key runs again as a key
  never seen; markers fenced before 0090 keep no expiry. The refusal carries
  `issue_id` and names `get_issue_work_context` as its `next`. On the same
  scripted fleet run the fenced share fell from 80% to 40%. The migration
  rebuilds the receipt table: at 200,000 receipts it takes about 1.4 s holding
  the write lock and doubles the file and the WAL; above about 700,000 receipts
  it passes the 5-second busy timeout, so migrate with the service stopped.
  `tests/test_idempotency_issue_fence.py` checks the fence over 3,000 generated
  histories and fails on fourteen deliberately too-narrow fences.

  Decision D17 (accepted, reversible by the owner): an issue's assignment or
  creator changing may fence only that issue's receipts, and a fenced marker may
  expire, if the build shows a replay never returns what its caller could no
  longer obtain; otherwise only the expiry. This change builds both, within the
  acts its documents list.
