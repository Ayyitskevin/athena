-- 0078_project_rooms: flavor rooms on a project floor.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0079_*.sql.
--
-- A room is a named area on the floor (Warehouse, Accounting), not a workflow.
-- It does not replace status, sprint, lease, or assignee. Deleting a room
-- unsits issues from that room (SET NULL); it does not close or unassign them.

CREATE TABLE project_rooms (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    blurb TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, slug)
);

ALTER TABLE issues ADD COLUMN room_id INTEGER REFERENCES project_rooms(id) ON DELETE SET NULL;
CREATE INDEX idx_issues_room_id ON issues(room_id);
