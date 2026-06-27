-- 0034_page_comments: discussion thread on a page (Mentor's Confluence half).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0035_*.sql.
-- The page twin of 0004_comments (issue comments): a page has had an audit trail
-- and history but no place to discuss it. Same shape as comments, keyed on the page.

CREATE TABLE page_comments (
    id         INTEGER PRIMARY KEY,
    -- the page this comment hangs off; the DB refuses a comment on no page
    page_id    INTEGER NOT NULL REFERENCES pages(id),
    -- who wrote it (a real user/agent); refuses an orphan author
    author_id  INTEGER NOT NULL REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The hot query is "all comments for this page, in order" — index the page.
CREATE INDEX idx_page_comments_page ON page_comments (page_id);
