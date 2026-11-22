-- 0085_desk_cursor_repair: rewind, once, every desk cursor already past the
-- newest event (spec A3, decision D14 of 2026-11-22).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0086_*.sql.
--
-- Until A3 the cursor command bounded `after_id` only by the SQLite integer
-- maximum, so one mistaken acknowledgement (say 999999) was stored, every
-- correction was refused as a rewind by 0073's advance-only trigger, and the
-- desk's events_since_cursor read 0 from then on. The command now refuses an
-- id past the newest event; this migration repairs the rows written before it.
--
-- The repair is the one deliberate exception to advance-only, which is why it
-- is a migration and not code: drop the trigger, clamp, recreate it exactly as
-- 0073 wrote it. The runner applies this file and its ledger row in a single
-- transaction, so no writer can observe the moment the trigger is absent.
--
-- Clamp target: MAX(activity.id), the newest position a reader can honestly
-- have acknowledged. 0073's CHECK (after_id > 0) forbids a zero position, so on
-- an empty trail (every stored position past the end) the row is removed: the
-- reader reads as "never acknowledged" (null), never as a blind zero.

DROP TRIGGER agent_cursors_advance_only;

UPDATE agent_cursors
SET after_id = (SELECT MAX(id) FROM activity),
    updated_at = datetime('now')
WHERE after_id > (SELECT MAX(id) FROM activity);

DELETE FROM agent_cursors
WHERE NOT EXISTS (SELECT 1 FROM activity);

CREATE TRIGGER agent_cursors_advance_only
BEFORE UPDATE ON agent_cursors
WHEN NEW.after_id < OLD.after_id
BEGIN
    SELECT RAISE(ABORT, 'a cursor may not move backward');
END;
