-- 0008_projects: a project groups related issues (e.g. "Website Redesign").
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0009_*.sql.

-- A project is a named container for work. name is UNIQUE and COLLATE NOCASE so
-- "Website" and "website" are the same project (no confusing near-duplicates);
-- this also gives create a clean 409 on a duplicate name. Every project is
-- created by a real user; the DB refuses orphans.
CREATE TABLE projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- An issue may belong to one project, or none. SQLite allows ADD COLUMN with a
-- REFERENCES clause only because the default is NULL — existing rows simply
-- start with no project (NULL = "no project / backlog"). No ON DELETE clause:
-- a project can't be deleted out from under its issues (NO ACTION) — there is
-- no delete-project path yet, and an issue must never point at a ghost project.
ALTER TABLE issues ADD COLUMN project_id INTEGER REFERENCES projects(id);
