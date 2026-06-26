-- 0024_project_statuses: per-project, configurable issue statuses.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0025.
--
-- The issue lifecycle was a single hard-coded global set (open/in_progress/done).
-- This lets each project define its OWN ordered statuses, each tagged with a
-- category (todo/doing/done) that drives board grouping and "is it closed?"
-- semantics — WITHOUT importing a Jira-style transition/validator engine. There is
-- no transition graph: any status can move to any other; the set is just the menu.
--
-- Backlog issues (no project) have no rows here; they use the built-in default set
-- (aegis/statuses.DEFAULT_STATUSES), which is also what every project is seeded
-- with. So nothing changes until a project customizes its statuses.
CREATE TABLE project_statuses (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    category   TEXT    NOT NULL CHECK (category IN ('todo', 'doing', 'done')),
    position   INTEGER NOT NULL,                          -- display/order within the project
    UNIQUE (project_id, name COLLATE NOCASE)              -- no two same-named statuses in a project
);

-- The hot read is "this project's statuses, in order".
CREATE INDEX idx_project_statuses_project ON project_statuses (project_id, position);

-- Seed every EXISTING project with the historical default lifecycle, so all of
-- their current issues (whose status is already one of these) stay valid.
INSERT INTO project_statuses (project_id, name, category, position)
    SELECT id, 'open', 'todo', 0 FROM projects;
INSERT INTO project_statuses (project_id, name, category, position)
    SELECT id, 'in_progress', 'doing', 1 FROM projects;
INSERT INTO project_statuses (project_id, name, category, position)
    SELECT id, 'done', 'done', 2 FROM projects;
