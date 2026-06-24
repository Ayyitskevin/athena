-- 0017_activity: an append-only record of who did what — the audit trail behind
-- "Grok closed AEGIS-88 is first-class history, not a mystery" (ARCHITECTURE).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0018_*.sql.

CREATE TABLE activity (
    id          INTEGER PRIMARY KEY,
    -- WHO acted. A real user; the DB refuses orphans. (Agents are users too, so
    -- "Grok" is just another actor_id.)
    actor_id    INTEGER NOT NULL REFERENCES users(id),
    -- WHAT they did: a controlled verb the app sets (created, changed_status,
    -- assigned, unassigned). Free TEXT, but only activity.py writes this column.
    verb        TEXT NOT NULL,
    -- WHAT it happened to, addressed polymorphically: ('issue', 12). No foreign
    -- key on target_id — it points into different tables depending on target_kind,
    -- and SQLite can't reference a kind-dependent table. The trail also
    -- intentionally OUTLIVES its target: deleting an issue must not erase the
    -- record that it existed and who closed it.
    target_kind TEXT NOT NULL,
    target_id   INTEGER NOT NULL,
    -- Human-readable specifics, e.g. "open → done" or the new assignee's name.
    -- Empty when the verb already says everything (created).
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Two read shapes: the global feed (newest first = ORDER BY id DESC, served by the
-- primary key in reverse) and one target's history (WHERE target_kind=? AND
-- target_id=?). This index serves the per-target lookup.
CREATE INDEX idx_activity_target ON activity (target_kind, target_id, id);
