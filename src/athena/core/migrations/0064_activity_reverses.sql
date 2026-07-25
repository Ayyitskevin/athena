-- 0064_activity_reverses: link a compensating event to the event it reverses.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0065_*.sql.
--
-- Athena's trail is append-only, so "undo" can never mean deleting or rewriting a
-- row. The model is COMPENSATION: undoing event N runs the inverse as a NEW,
-- fully audited forward command, and the event that command records carries
-- reverses_event_id = N. History gains two rows (the original and its reversal);
-- nothing is erased, and run replay/lineage stay exact because the reversal is an
-- ordinary event of the run that performed it.
--
-- NULL for every event that is not a reversal — which is every event recorded
-- before this migration and the overwhelming majority after it, so the trail is
-- unchanged unless an undo actually happens.
ALTER TABLE activity ADD COLUMN reverses_event_id INTEGER REFERENCES activity(id);

-- One reversal per event, enforced by the DATABASE rather than by a check-then-act
-- read in the engine: two concurrent undos of the same event would both see "not
-- yet reversed" and both compensate, applying the inverse twice. SQLite's
-- single-writer reservation orders them and this index makes the loser fail
-- closed. Partial, so the NULL majority carries no index entry.
CREATE UNIQUE INDEX idx_activity_reverses_event_unique
    ON activity (reverses_event_id) WHERE reverses_event_id IS NOT NULL;
