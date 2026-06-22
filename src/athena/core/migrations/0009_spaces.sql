-- 0009_spaces: a space is a top-level container for documentation pages, the
-- Mentor (Confluence) equivalent of an Aegis project. FORWARD-ONLY: once applied
-- anywhere, never edit this file — add 0010_*.sql.

-- A space is identified by a short KEY (e.g. "ENG", "OPS"), not just a name. The
-- key is the canonical identity: pages will later live at URLs built from it
-- (/mentor/ENG/...), so it is UNIQUE and COLLATE NOCASE ("eng" == "ENG", no
-- confusing near-duplicates) and gives create a clean 409 on a duplicate. name
-- is a human display label (required, not unique). Every space is created by a
-- real user; the DB refuses orphans.
CREATE TABLE spaces (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
