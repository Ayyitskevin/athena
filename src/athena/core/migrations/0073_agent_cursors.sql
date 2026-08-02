-- 0073_agent_cursors: a per-reader position in the append-only trail.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0074_*.sql.
--
-- An agent that wants to know "what changed since I last looked" has had to
-- remember the answer itself: GET /events takes an `after` cursor, but nothing
-- durably held one. A process that restarts, or a fresh context continuing
-- someone else's work, had no way to resume except by guessing a timestamp or
-- re-reading everything. This table is that memory, owned by the reader.
--
-- PERSONAL STATE, NOT HISTORY. A read receipt is the reader's own bookkeeping —
-- it records nothing about the fleet and emits no activity event (the category
-- and its criteria are documented in COMMAND_MIGRATION.md: owner-scoped writes,
-- no audit events, exactly one data-module writer). The trail says what
-- happened; this says only how far one reader has drained it.
--
-- ADVANCE-ONLY. A cursor moves forward or not at all. Rewinding one would let a
-- reader claim it never saw an event it had already acknowledged, which is a
-- claim about history rather than about position — exactly the shape Athena
-- refuses everywhere else. The command refuses it for a clean 409; the trigger
-- below refuses it again for any writer that skips the command.
--
-- WHAT A CURSOR IS NOT: not a lock, not a lease, not a queue, and not a
-- delivery guarantee. Two readers may hold the same position; a cursor grants
-- no exclusivity and reserves no work. It is a bookmark.

CREATE TABLE agent_cursors (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    -- Closed vocabulary: a cursor NAME is a promise about what the position
    -- means, so a new one is a deliberate migration rather than a free string
    -- any caller can mint.
    name       TEXT    NOT NULL CHECK (name IN ('desk')),
    -- The highest activity id this reader has acknowledged. Not a foreign key
    -- to activity(id) on purpose: the position survives as a NUMBER even for a
    -- trail whose rows a portability import renumbered, and a reader is allowed
    -- to acknowledge "everything up to here" without that id still existing.
    after_id   INTEGER NOT NULL CHECK (after_id > 0),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, name)
);

-- A cursor only ever moves forward. Equal is allowed (an idempotent re-ack of
-- the same position is a no-op, not an error); smaller is refused.
CREATE TRIGGER agent_cursors_advance_only
BEFORE UPDATE ON agent_cursors
WHEN NEW.after_id < OLD.after_id
BEGIN
    SELECT RAISE(ABORT, 'a cursor may not move backward');
END;

-- The identity of a cursor is immutable: moving a position between readers or
-- renaming what it means would silently change the claim the row makes.
CREATE TRIGGER agent_cursors_identity_immutable
BEFORE UPDATE ON agent_cursors
WHEN NEW.user_id IS NOT OLD.user_id OR NEW.name IS NOT OLD.name
BEGIN
    SELECT RAISE(ABORT, 'cursor identity is immutable');
END;
