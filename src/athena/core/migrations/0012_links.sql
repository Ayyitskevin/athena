-- 0012_links: the cross-link index — the one-roof payoff (issue<->page refs).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0013_*.sql.

-- A directed reference from one thing to another, extracted from a source's body
-- text (an author writes [[issue:42]] or [[page:7]]). One row per distinct
-- (source -> target) reference. The kinds are polymorphic ('issue' | 'page'), so
-- this table can't use foreign keys — a column can't reference two tables. We
-- keep integrity instead by RE-DERIVING a source's rows from its body on every
-- write (core/links.sync_links) and resolving a target's existence at READ time:
-- a reference to something not (yet) created simply isn't shown as a live link,
-- and is rendered "broken" inline. Dangling rows are harmless and let a target
-- created later light up its backlinks retroactively.
CREATE TABLE links (
    source_kind TEXT    NOT NULL,   -- 'issue' | 'page'
    source_id   INTEGER NOT NULL,
    target_kind TEXT    NOT NULL,   -- 'issue' | 'page'
    target_id   INTEGER NOT NULL,
    PRIMARY KEY (source_kind, source_id, target_kind, target_id)
);

-- The marquee query is "what references THIS?" (backlinks), keyed by target.
CREATE INDEX idx_links_target ON links (target_kind, target_id);
