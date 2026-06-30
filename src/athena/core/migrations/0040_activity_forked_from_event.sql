-- 0040_activity_forked_from_event: record the exact parent event a run forked from.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it -- add 0041.
--
-- parent_run_id (0030) says which run spawned this run. This column pins the fork
-- point inside that parent run: the activity event after whose shared prefix the
-- child diverged. The value is stamped from X-Athena-Fork-From-Event, alongside
-- X-Athena-Run and X-Athena-Parent-Run, so run forking remains metadata on the
-- append-only log rather than a separate mutable run table. NULL means "not a fork"
-- or "fork point unknown", preserving all existing browser and untagged actions.
ALTER TABLE activity ADD COLUMN forked_from_event_id INTEGER;

-- Lineage views can answer "which child runs forked from event N?" without scanning
-- the whole trail. NULLs are harmless here; SQLite's btree handles them cheaply.
CREATE INDEX idx_activity_forked_from_event
    ON activity (forked_from_event_id);
