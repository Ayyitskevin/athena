-- 0010_pages: a page is a single document inside a space — the unit of value in
-- Mentor (Confluence). FORWARD-ONLY: once applied anywhere, never edit this file
-- — add 0011_*.sql.

-- Every page belongs to exactly one space (space_id NOT NULL) and may optionally
-- nest under a parent page in that same space (parent_id NULL = a top-level page).
-- Neither FK has an ON DELETE clause (NO ACTION): a space can't be deleted out
-- from under its pages, nor a parent page out from under its children — there is
-- no delete path yet, and a page must never point at a ghost. The parent must be
-- in the SAME space; that cross-space tree rule is enforced at the API boundary
-- (the FK alone can't express it). body defaults to '' so a page can start empty.
CREATE TABLE pages (
    id          INTEGER PRIMARY KEY,
    space_id    INTEGER NOT NULL REFERENCES spaces(id),
    parent_id   INTEGER REFERENCES pages(id),
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Pages are almost always read a whole space at a time (to render the tree), so
-- index the space they belong to.
CREATE INDEX idx_pages_space ON pages(space_id);
