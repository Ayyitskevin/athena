-- 0054_issue_leases: the delegation claim/lease protocol. An issue can be delegated to
-- more than one agent (the contributor set), which risks two agents silently working the
-- same issue. A LEASE is the exclusive "I am actively working this right now" token —
-- distinct from the assignee (accountable owner) and the contributor set (who MAY work
-- it). FORWARD-ONLY: once applied anywhere, never edit this file — add 0055_*.sql.

-- At most one ACTIVE lease per issue (PRIMARY KEY issue_id): claiming acquires it,
-- completing releases it, and a lease that outlives its window is reclaimable — so a
-- crashed agent's claim cannot pin the work forever. holder_id is the claimant;
-- expires_at bounds the lease so "active" is a pure function of the clock, needing no
-- background sweeper. No ON DELETE on issue_id: there is no issue hard-delete path, and a
-- lease must never point at a ghost issue (the command clears the lease on complete).
CREATE TABLE issue_leases (
    issue_id   INTEGER PRIMARY KEY REFERENCES issues(id),
    holder_id  INTEGER NOT NULL REFERENCES users(id),
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL   -- the lease window; a lease is active only while now < this
);

-- "Which of MY leases are still active" and "who holds this issue" both key on the
-- holder; index it so the reclaim/expiry checks stay cheap as the fleet grows.
CREATE INDEX idx_issue_leases_holder ON issue_leases (holder_id);
