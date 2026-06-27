-- 0029_activity_run_id: stamp each event with the RUN it belongs to.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0030.
--
-- A "run" is a unit of agent work. Until now runs could only be RECONSTRUCTED from
-- the trail by time gap (a heuristic — see activity.reconstruct_runs). This column
-- makes them DETERMINISTIC: a client (an agent, or any integration) sets an
-- X-Athena-Run header on the requests that make up one run, and every event recorded
-- during those requests is stamped with that id. Replaying a known run is then exact
-- — "give me run R's events" — not a guess. Opaque client-supplied text (a UUID, a
-- job id, whatever the caller uses); NULL for everything not tagged (all existing
-- rows, and every human browser action), so the trail is unchanged unless a run id
-- is explicitly supplied.
ALTER TABLE activity ADD COLUMN run_id TEXT;

-- The deterministic read is "all events of run R" — index the run end so that
-- replay-by-id doesn't scan the whole trail. NULLs aren't indexed by SQLite's
-- default, which is exactly right: untagged rows add nothing to this index.
CREATE INDEX idx_activity_run ON activity (run_id);
