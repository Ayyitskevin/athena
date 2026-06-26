-- 0022_attachments: file attachments for issues and pages.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0023.
--
-- The blob lives on the filesystem (under ATHENA_ATTACH_DIR); this table is the
-- one home for its METADATA and the only thing that addresses it. The link is
-- `stored_name` — a server-generated random name on disk — so the client's
-- filename is never used as a path (no traversal) and is kept only for display.
-- target is polymorphic ('issue' | 'page'), like activity/links: one table serves
-- both modules, so no foreign key on target_id (it points into different tables by
-- kind). uploaded_by is a real user; the DB refuses an orphan.
CREATE TABLE attachments (
    id           INTEGER PRIMARY KEY,
    target_kind  TEXT    NOT NULL,                       -- 'issue' | 'page'
    target_id    INTEGER NOT NULL,
    filename     TEXT    NOT NULL,                       -- original name, DISPLAY ONLY (never a path)
    content_type TEXT    NOT NULL,
    byte_size    INTEGER NOT NULL,
    sha256       TEXT    NOT NULL,                       -- integrity / dedup visibility
    stored_name  TEXT    NOT NULL UNIQUE,                -- random on-disk name; the safe handle
    uploaded_by  INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The hot query is "this issue's / this page's attachments".
CREATE INDEX idx_attachments_target ON attachments (target_kind, target_id);
