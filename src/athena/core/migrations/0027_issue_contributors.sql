-- 0027_issue_contributors: additional people/agents working an issue.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0028.
--
-- An issue has ONE primary assignee (the accountable owner, on issues.assignee_id)
-- and any number of CONTRIBUTORS: extra teammates working it. Delegating an issue —
-- to a human OR a token-driven agent — is adding them as a contributor while the
-- assignee stays accountable (the agent-as-teammate model). The composite PK makes a
-- (issue, user) pair unique and the add idempotent. added_by records WHO delegated,
-- for the audit trail. Foreign keys refuse an orphan; there is no ON DELETE because
-- issues/users are not deleted in this product.
CREATE TABLE issue_contributors (
    issue_id  INTEGER NOT NULL REFERENCES issues(id),
    user_id   INTEGER NOT NULL REFERENCES users(id),
    added_by  INTEGER NOT NULL REFERENCES users(id),
    added_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (issue_id, user_id)
);

-- The hot read is "who contributes to this issue" — index the issue end.
CREATE INDEX idx_issue_contributors_issue ON issue_contributors (issue_id);
