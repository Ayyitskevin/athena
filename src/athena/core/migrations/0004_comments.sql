-- 0004_comments: discussion/activity thread on an issue.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0005_*.sql.

CREATE TABLE comments (
    id         INTEGER PRIMARY KEY,
    -- the issue this comment hangs off; the DB refuses a comment on no issue
    issue_id   INTEGER NOT NULL REFERENCES issues(id),
    -- who wrote it (a real user/agent); refuses an orphan author
    author_id  INTEGER NOT NULL REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The hot query is "all comments for this issue, in order" — index the issue.
CREATE INDEX idx_comments_issue ON comments (issue_id);
