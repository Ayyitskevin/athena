-- 0026_saved_filters: named, reusable issue queries owned by a user.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0027.
--
-- A saved filter is "query-lite": a small set of criteria (status, priority,
-- assignee, label, project, search) stored as a JSON object, that — run together —
-- return the issues matching ALL of them. Running a filter funnels through the one
-- issue-filtering path (issues.list_issues), so a saved query and an ad-hoc query
-- can never disagree on what matches.
--
-- Filters are PERSONAL: each belongs to the user who created it (owner_id), the
-- same scoping model as the notification inbox. A user can't name two filters the
-- same thing (UNIQUE per owner, case-insensitive), but two users can each have a
-- "My open issues".

CREATE TABLE saved_filters (
    id         INTEGER PRIMARY KEY,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    name       TEXT    NOT NULL,
    -- The criteria, a JSON object of optional keys. '{}' is a legal empty filter
    -- ("every issue"). The shape is validated at the boundary, not by the DB.
    criteria   TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    -- One name per owner, case-insensitive ("Triage" == "triage"); different owners
    -- may reuse a name.
    UNIQUE (owner_id, name COLLATE NOCASE)
);

-- The hot read is "this user's filters, by name" — index the owner end.
CREATE INDEX idx_saved_filters_owner ON saved_filters (owner_id, name COLLATE NOCASE);
