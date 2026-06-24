-- 0016_issue_links: typed dependencies BETWEEN issues (blocks / relates).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0017_*.sql.
--
-- Distinct from the `links` table (0012): that one indexes [[issue:N]] mentions
-- parsed from body TEXT and is re-derived on every write. THIS table holds
-- explicit, user-created relationships — a person clicks "blocks" — so it has its
-- own row identity and real foreign keys. Both ends are issues (one table), so
-- unlike the polymorphic links table it CAN reference, and an issue's edges
-- vanish with it (ON DELETE CASCADE) — a dangling dependency is meaningless.
--
-- Two stored kinds, three user-facing relations:
--   'blocks'  is DIRECTED: a row (from_id, to_id, 'blocks') means from_id blocks
--             to_id. "blocked by" is just this row read from the other end, so it
--             is NEVER stored separately.
--   'relates' is SYMMETRIC: stored ONCE, normalized by the data layer to
--             from_id < to_id, so A-relates-B and B-relates-A are one row.
CREATE TABLE issue_links (
    from_id    INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    to_id      INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('blocks', 'relates')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (from_id, to_id, kind),
    CHECK (from_id <> to_id)
);

-- The PK already indexes the from_id end ("what does this issue block / relate
-- to"). The other hot query walks the to_id end ("what blocks this issue" — the
-- warn-on-close lookup), so add the reverse index.
CREATE INDEX idx_issue_links_to ON issue_links (to_id, kind);
