-- 0074_issue_drafts: an author's unsaved work in progress, per user per issue.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0075_*.sql.
--
-- The issue twin of page_drafts (0071), and the store the documented
-- issue/page conflict asymmetry was waiting on: a page conflict could put the
-- winner's text in the fields because the loser's copy was safe in their
-- draft, while an issue conflict had to keep the loser's text in the form and
-- admit it was not saved anywhere. This table lets both editors behave the
-- same way.
--
-- Every design decision is inherited from 0071, because the sentence deciding
-- them is the same: A DRAFT IS USER-PRIVATE STATE, NOT CONTENT. So a draft:
--
--   * lives in its OWN table — no write path to `issues` skips the audited
--     command, and a draft simply is not a write to the issue;
--   * records NO activity event — autosave is not a lifecycle moment;
--   * is readable ONLY by its owner — not admins, not project members;
--   * is not exported — portability carries content, not scratch copies;
--   * never becomes the issue by itself — only an explicit save through
--     issue_commands.update_issue does that.
--
-- One row per (issue, author): a draft is "where I am on this issue", not a
-- history. ON DELETE CASCADE both ways, so a purged issue or a removed user
-- leaves no private ghost behind. Bounds match 0071's: the drafts are twins,
-- and an author moving between the two editors should hit the same walls.

CREATE TABLE issue_drafts (
    issue_id   INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL DEFAULT '' CHECK (length(title) <= 500),
    body       TEXT    NOT NULL DEFAULT '' CHECK (length(body) <= 200000),
    -- The issue's ETag when this draft was last taken; what makes a stale
    -- draft VISIBLE rather than silently clobbering what landed in between.
    based_on   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (issue_id, owner_id)
);

-- "What am I part-way through?" — one author's drafts, freshest first.
CREATE INDEX idx_issue_drafts_owner ON issue_drafts (owner_id, updated_at DESC);
