-- A saved bare title keeps its original page ID. Page IDs are never recycled;
-- a deleted target therefore stays dangling just like an explicit [[page:N]].
-- NULL is a deliberately withheld legacy/imported binding. A fresh, unresolved
-- token has no row, so a later source synchronization can resolve it.
CREATE TABLE title_link_bindings (
    source_kind TEXT NOT NULL CHECK (source_kind IN ('issue', 'page')),
    source_id INTEGER NOT NULL,
    title_key TEXT NOT NULL,
    target_id INTEGER,
    PRIMARY KEY (source_kind, source_id, title_key)
) WITHOUT ROWID;

-- Freeze just the evidence available at upgrade. The old graph did not store
-- which spelling supplied an edge; a subsequent rename must not change what
-- can be proved. On the first source edit the Python token grammar reconciles
-- the old body before considering the edited one, then discards this snapshot.
CREATE TABLE legacy_title_link_sources (
    source_kind TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    page_edges TEXT NOT NULL,
    PRIMARY KEY (source_kind, source_id)
) WITHOUT ROWID;

WITH sources(kind, id, body) AS (
    SELECT 'issue', id, body FROM issues WHERE instr(body, '[[') > 0
    UNION ALL
    SELECT 'page', id, body FROM pages WHERE instr(body, '[[') > 0
)
INSERT INTO legacy_title_link_sources
SELECT kind, id, body, (
    SELECT json_group_array(json_object('id', l.target_id, 'titles', json((
        SELECT json_group_array(title) FROM (
            SELECT title FROM pages WHERE id = l.target_id
            UNION
            SELECT title FROM page_versions WHERE page_id = l.target_id
        )
    ))))
    FROM links l
    WHERE l.source_kind = sources.kind AND l.source_id = sources.id
      AND l.target_kind = 'page'
)
FROM sources;

CREATE TRIGGER issue_delete_title_bindings AFTER DELETE ON issues BEGIN
    DELETE FROM title_link_bindings WHERE source_kind = 'issue' AND source_id = OLD.id;
    DELETE FROM legacy_title_link_sources WHERE source_kind = 'issue' AND source_id = OLD.id;
END;
CREATE TRIGGER page_delete_title_bindings AFTER DELETE ON pages BEGIN
    DELETE FROM title_link_bindings WHERE source_kind = 'page' AND source_id = OLD.id;
    DELETE FROM legacy_title_link_sources WHERE source_kind = 'page' AND source_id = OLD.id;
END;
