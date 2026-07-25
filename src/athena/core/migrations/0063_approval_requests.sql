-- 0063_approval_requests: human-in-the-loop gates for risky agent actions.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0064_*.sql.
--
-- VISION.md's Intervene step promises the operator can "approve/reject risky
-- actions (human-in-the-loop)" and steer by exception. Until now the only gate was
-- the per-project blocked-close policy (0060), which can only refuse -- there was
-- no way to say "ask me first, and I'll decide".
--
-- MECHANISM: GATE + RETRY, not deferred execution. A gated command REFUSES and
-- records a pending request naming who wanted to do what to which target. When the
-- operator approves, the agent RETRIES the same write and it succeeds, consuming
-- the approval. Athena never serializes an agent's mutation and replays it later on
-- the agent's behalf: doing so would re-execute a stale payload under authorization
-- evaluated at a different moment, which is a new trust boundary rather than a
-- feature. The retry re-validates everything -- visibility, policy, budget, ETag --
-- so an approval authorizes an INTENT, never a stored side effect.
--
-- SINGLE-USE: consuming an approval is part of the retry's own transaction, so an
-- approval can never become a standing permission. One approval, one action.
--
-- OPT-IN: a user with no policy row is ungated, mirroring budgets (0062) and the
-- blocked-close policy (0060). Applying this migration changes nothing until an
-- operator gates someone.

-- Which action kinds require approval for which user. A row means "gated".
CREATE TABLE agent_approval_policies (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- A dotted action kind, e.g. 'issue.close'. Kept as free text rather than a
    -- CHECK so a new gated action needs code, not a migration; the command layer
    -- owns the vocabulary (core.approvals.ACTION_KINDS).
    action_kind TEXT NOT NULL,
    set_by      INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, action_kind)
);

CREATE TABLE approval_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action_kind   TEXT NOT NULL,
    -- What the action would touch. Kept as kind+id (not a payload blob) precisely
    -- because Athena never replays the mutation: the operator approves an intent
    -- against a target, and the agent's retry supplies the actual change.
    target_kind   TEXT NOT NULL,
    target_id     INTEGER NOT NULL,
    requested_by  INTEGER NOT NULL REFERENCES users(id),
    -- The run the refused attempt was stamped with, so an approval joins the
    -- replay/lineage story rather than floating outside it. NULL for untagged work.
    run_id        TEXT,
    state         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'approved', 'rejected')),
    decided_by    INTEGER REFERENCES users(id),
    decided_at    TEXT,
    decision_note TEXT,
    -- Set when an approved request is spent by a successful retry. An approved,
    -- unconsumed row is the only thing that opens the gate.
    consumed_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- At most ONE live (pending or approved-unconsumed) request per
-- (requester, action kind, target): a gated agent retrying while it waits must not
-- pile up duplicate asks in the operator's queue, and two approvals for the same
-- intent must never both be spendable.
CREATE UNIQUE INDEX idx_approval_requests_live
    ON approval_requests (requested_by, action_kind, target_kind, target_id)
    WHERE state != 'rejected' AND consumed_at IS NULL;

-- The operator's queue: pending first, newest first.
CREATE INDEX idx_approval_requests_state ON approval_requests (state, id DESC);
