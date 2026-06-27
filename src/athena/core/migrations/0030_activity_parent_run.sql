-- 0030_activity_parent_run: link a run to the run that spawned it (lineage).
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0031.
--
-- run_id (0029) tells you WHICH run an event belongs to. This tells you that run's
-- PARENT: when one run delegates work and a second run carries it out, the child's
-- events are stamped with the parent's run id (the X-Athena-Parent-Run header,
-- mirroring X-Athena-Run). A run thus becomes a node in a tree — "run forking" — and
-- the lineage is reconstructed by reading (run_id, parent_run_id) off the trail, with
-- no stored "run" row: a run stays a derived lens, exactly as 0029 left it. Opaque
-- client text; NULL for every untagged or top-level run, so the trail is unchanged
-- unless a parent is explicitly supplied.
ALTER TABLE activity ADD COLUMN parent_run_id TEXT;

-- The lineage read is "the events whose parent run is R" — i.e. find R's children.
-- Index the parent end so walking the tree downward doesn't scan the whole trail.
CREATE INDEX idx_activity_parent_run ON activity (parent_run_id);
