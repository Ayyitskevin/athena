-- 0042_activity_visibility_scope: preserve the project access context of each
-- issue event instead of deriving all history access from the issue's current home.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0043_*.sql.
--
-- Existing/imported rows cannot be assigned an honest historical project from the
-- data Athena stored before this migration. They therefore remain restricted by
-- default and are available only to the admin/ungated forensic view. Native
-- activity.record writes explicitly clear the flag after resolving their scope.
ALTER TABLE activity ADD COLUMN visibility_restricted INTEGER NOT NULL DEFAULT 1
    CHECK (visibility_restricted IN (0, 1));

-- A bare SQLite INTEGER PRIMARY KEY may be reused after its highest row is deleted.
-- Give each project generation an opaque, immutable identity so deleting private project
-- 9 and later creating a public project 9 can never re-authorize the old private facts.
ALTER TABLE projects ADD COLUMN activity_scope_key TEXT;
UPDATE projects SET activity_scope_key = lower(hex(randomblob(16)));
CREATE UNIQUE INDEX idx_projects_activity_scope_key
    ON projects (activity_scope_key);
CREATE TRIGGER projects_activity_scope_key_required
BEFORE INSERT ON projects
WHEN NEW.activity_scope_key IS NULL OR length(NEW.activity_scope_key) <> 32
BEGIN
    SELECT RAISE(ABORT, 'project activity scope key required');
END;
CREATE TRIGGER projects_activity_scope_key_immutable
BEFORE UPDATE OF activity_scope_key ON projects
WHEN NEW.activity_scope_key IS NOT OLD.activity_scope_key
BEGIN
    SELECT RAISE(ABORT, 'project activity scope key is immutable');
END;

-- An event may contain facts from more than one project. Store immutable project
-- generation keys, not reusable row ids. There is deliberately no FK to projects:
-- the append-only audit trail and its access envelope outlive a deleted project.
CREATE TABLE activity_visibility_projects (
    event_id          INTEGER NOT NULL REFERENCES activity(id),
    project_scope_key TEXT NOT NULL,
    PRIMARY KEY (event_id, project_scope_key)
);

CREATE INDEX idx_activity_visibility_projects_project
    ON activity_visibility_projects (project_scope_key, event_id);

-- Activity targets are append-only identities even when their live container row is
-- deletable. Reserve every project/space/page id ever used by either the entity or an
-- event, and allocate new rows from a monotonic sequence. This prevents SQLite's
-- default highest-row-id reuse from binding old private activity to a new public row.
CREATE TABLE activity_target_id_reservations (
    target_kind TEXT NOT NULL CHECK (target_kind IN ('project', 'space', 'page')),
    target_id   INTEGER NOT NULL CHECK (target_id > 0),
    PRIMARY KEY (target_kind, target_id)
);

INSERT OR IGNORE INTO activity_target_id_reservations
    SELECT 'project', id FROM projects;
INSERT OR IGNORE INTO activity_target_id_reservations
    SELECT 'space', id FROM spaces;
INSERT OR IGNORE INTO activity_target_id_reservations
    SELECT 'page', id FROM pages;
INSERT OR IGNORE INTO activity_target_id_reservations
    SELECT target_kind, target_id FROM activity
    WHERE target_kind IN ('project', 'space', 'page') AND target_id > 0;

CREATE TABLE activity_target_id_sequences (
    target_kind TEXT PRIMARY KEY CHECK (target_kind IN ('project', 'space', 'page')),
    next_id     INTEGER NOT NULL CHECK (next_id > 0)
);

INSERT INTO activity_target_id_sequences
    SELECT 'project', COALESCE(MAX(target_id), 0) + 1
    FROM activity_target_id_reservations WHERE target_kind = 'project';
INSERT INTO activity_target_id_sequences
    SELECT 'space', COALESCE(MAX(target_id), 0) + 1
    FROM activity_target_id_reservations WHERE target_kind = 'space';
INSERT INTO activity_target_id_sequences
    SELECT 'page', COALESCE(MAX(target_id), 0) + 1
    FROM activity_target_id_reservations WHERE target_kind = 'page';

CREATE TRIGGER reserve_project_activity_target_id
AFTER INSERT ON projects
BEGIN
    INSERT INTO activity_target_id_reservations VALUES ('project', NEW.id);
    UPDATE activity_target_id_sequences
       SET next_id = MAX(next_id, NEW.id + 1) WHERE target_kind = 'project';
END;
CREATE TRIGGER reserve_space_activity_target_id
AFTER INSERT ON spaces
BEGIN
    INSERT INTO activity_target_id_reservations VALUES ('space', NEW.id);
    UPDATE activity_target_id_sequences
       SET next_id = MAX(next_id, NEW.id + 1) WHERE target_kind = 'space';
END;
CREATE TRIGGER reserve_page_activity_target_id
AFTER INSERT ON pages
BEGIN
    INSERT INTO activity_target_id_reservations VALUES ('page', NEW.id);
    UPDATE activity_target_id_sequences
       SET next_id = MAX(next_id, NEW.id + 1) WHERE target_kind = 'page';
END;
CREATE TRIGGER reserve_activity_target_id
AFTER INSERT ON activity
WHEN NEW.target_kind IN ('project', 'space', 'page') AND NEW.target_id > 0
BEGIN
    INSERT OR IGNORE INTO activity_target_id_reservations
        VALUES (NEW.target_kind, NEW.target_id);
    UPDATE activity_target_id_sequences
       SET next_id = MAX(next_id, NEW.target_id + 1)
       WHERE target_kind = NEW.target_kind;
END;
