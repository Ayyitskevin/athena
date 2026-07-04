-- 0041_activity_imported_at: mark the activity rows that entered via a portability
-- import so foreign history is auditable and can never be mistaken for natively
-- recorded provenance.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0042_*.sql.
--
-- WHY: an import bundle is untrusted input. A natively recorded event (activity.record)
-- gets the server clock and the ambient run context; an imported event carries the
-- bundle's own verb/timestamp/run tagging. Without a marker, an imported
-- "admin approved X" row is byte-identical to one the app recorded. This column makes
-- "was this row imported, and when" a queryable fact of the trail: NULL for every
-- app-recorded event, the import instant for every replayed one. The mapper also
-- neutralizes the imported run fields (run_id/parent_run_id/forked_from_event_id are
-- written NULL) so a bundle cannot splice forged events into a native run's replay.
ALTER TABLE activity ADD COLUMN imported_at TEXT;

-- Serve the "show me everything a given import brought in" audit query without carrying
-- an index entry for the app-recorded majority (imported_at IS NULL).
CREATE INDEX idx_activity_imported_at
    ON activity (imported_at) WHERE imported_at IS NOT NULL;
