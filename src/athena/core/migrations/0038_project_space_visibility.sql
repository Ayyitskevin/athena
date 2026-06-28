-- 0038_project_space_visibility: the schema for private projects and spaces.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0039.
--
-- Athena has been all-public: every read is open, even to signed-out visitors.
-- This adds the OPTION of privacy. A project/space carries a `visibility`:
--   'public'  (the default) — anyone may read it, exactly as before; nothing changes
--   'private' — only its members, its creator, and admins may read it
-- and a membership join table per container (the project/space twins of
-- 0027_issue_contributors). Marking something private and managing members come in a
-- later slice; THIS migration only lays the schema, and because the column defaults
-- to 'public', the whole install behaves identically until a row is actually flipped.
--
-- ON DELETE CASCADE on the container end so deleting a project/space (which the app
-- already allows once it is empty of issues/pages) sweeps its membership rows with
-- it rather than leaving orphans or blocking the delete on a foreign key.

ALTER TABLE projects ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'
    CHECK (visibility IN ('public', 'private'));

ALTER TABLE spaces ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'
    CHECK (visibility IN ('public', 'private'));

CREATE TABLE project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- who granted access (for the audit trail); nullable so a system/seed grant
    -- need not name an actor. No FK action needed beyond the default RESTRICT.
    added_by   INTEGER REFERENCES users(id),
    added_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, user_id)
);

-- The hot reads are "members of this project" (the PK's leading column covers it)
-- and "which private projects can this user see" — index the user end for the latter.
CREATE INDEX idx_project_members_user ON project_members (user_id);

CREATE TABLE space_members (
    space_id  INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_by  INTEGER REFERENCES users(id),
    added_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, user_id)
);

CREATE INDEX idx_space_members_user ON space_members (user_id);
