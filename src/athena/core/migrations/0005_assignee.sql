-- 0005_assignee: who is responsible for an issue (optional).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0006_*.sql.

-- A nullable owner: an issue may have no assignee (NULL = "Unassigned"), or
-- point at a real user. SQLite allows ADD COLUMN with a REFERENCES clause only
-- because the default is NULL — existing rows simply start unassigned.
ALTER TABLE issues ADD COLUMN assignee_id INTEGER REFERENCES users(id);
