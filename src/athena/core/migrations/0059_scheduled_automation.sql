-- 0059_scheduled_automation: bounded UTC schedule triggers beside event rules.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it -- add 0060.
--
-- Event rules keep their activity cursor and exact behavior.  A scheduled rule owns a
-- canonical UTC first slot and optional fixed repeat interval.  next_scheduled_at is
-- the durable cursor for that rule; the runner advances it while claiming a slot in
-- the same write transaction, so a restart cannot claim the same slot twice.
ALTER TABLE automation_rules
    ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'event'
    CHECK (trigger_type IN ('event', 'schedule'));
ALTER TABLE automation_rules ADD COLUMN schedule_at TEXT;
ALTER TABLE automation_rules
    ADD COLUMN schedule_every_seconds INTEGER
    CHECK (
        schedule_every_seconds IS NULL
        OR schedule_every_seconds BETWEEN 60 AND 31536000
    );
ALTER TABLE automation_rules ADD COLUMN next_scheduled_at TEXT;
ALTER TABLE automation_rules ADD COLUMN last_scheduled_for TEXT;
ALTER TABLE automation_rules
    ADD COLUMN schedule_missed_count INTEGER NOT NULL DEFAULT 0
    CHECK (schedule_missed_count >= 0);
ALTER TABLE automation_rules
    ADD COLUMN last_schedule_target_count INTEGER
    CHECK (last_schedule_target_count IS NULL OR last_schedule_target_count >= 0);
ALTER TABLE automation_rules
    ADD COLUMN last_schedule_overflow_count INTEGER NOT NULL DEFAULT 0
    CHECK (last_schedule_overflow_count >= 0);
-- Keep the bounded due-rule scan fair when malformed rows are skipped.  Advancing this
-- operational cursor only changes where the next bounded scan begins; per-rule slot
-- cursors and unique receipts remain the execution source of truth.
CREATE TABLE automation_schedule_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    scan_cursor INTEGER NOT NULL DEFAULT 0 CHECK (scan_cursor >= 0)
);
INSERT INTO automation_schedule_state (id, scan_cursor) VALUES (1, 0);

-- One durable record per claimed UTC slot.  There is deliberately no foreign key from
-- rule_id: deleting a rule must not delete the historical proof that it ran.  The
-- lifecycle command marks unfinished rows cancelled before removing the live rule.
CREATE TABLE automation_schedule_firings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         INTEGER NOT NULL,
    scheduled_for   TEXT NOT NULL,
    trigger_count   INTEGER NOT NULL DEFAULT 0 CHECK (trigger_count >= 0),
    overflow_count  INTEGER NOT NULL DEFAULT 0 CHECK (overflow_count >= 0),
    missed_slots    INTEGER NOT NULL DEFAULT 0 CHECK (missed_slots >= 0),
    state           TEXT NOT NULL
                    CHECK (state IN ('pending', 'completed', 'failed', 'cancelled')),
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    UNIQUE (rule_id, scheduled_for)
);

-- A target snapshot makes a slot deterministic across restarts: targets that appear
-- later never join an old occurrence, and targets that stop matching are still handled
-- by the command against their current state.  Each row owns one synthetic schedule
-- trigger event.  The existing action executor then keeps its event-derived run id and
-- parent/fork lineage unchanged.  State is a separate receipt because a valid no-op
-- action need not emit an activity row.
CREATE TABLE automation_schedule_occurrences (
    firing_id        INTEGER NOT NULL
                     REFERENCES automation_schedule_firings(id) ON DELETE CASCADE,
    issue_id         INTEGER NOT NULL,
    trigger_event_id INTEGER NOT NULL UNIQUE REFERENCES activity(id),
    state            TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'completed', 'failed', 'cancelled')),
    attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error       TEXT,
    last_attempt_at  TEXT,
    completed_at     TEXT,
    PRIMARY KEY (firing_id, issue_id)
);

CREATE INDEX idx_automation_schedule_due
    ON automation_rules (trigger_type, enabled, next_scheduled_at, id);
CREATE INDEX idx_automation_schedule_occurrence_work
    ON automation_schedule_occurrences (state, firing_id, issue_id);
