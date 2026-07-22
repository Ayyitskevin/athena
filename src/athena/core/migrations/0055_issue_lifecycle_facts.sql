-- 0055_issue_lifecycle_facts: append trustworthy, event-time issue lifecycle
-- semantics beside the activity trail for bounded throughput metrics.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0056_*.sql.
--
-- Legacy activity text cannot be classified honestly: project status categories
-- and users.is_agent are mutable, and old creation events omit their initial
-- status. New commands therefore snapshot the small set of typed facts needed by
-- metrics in the same transaction as their activity event. There is deliberately
-- no backfill and no FK from project scope keys: audit envelopes outlive projects.
-- Issue row ids are audit identities, not reusable storage slots. Reserve every
-- issue id already present in either live state or the activity trail, then allocate
-- future ids from one transactional sequence. This prevents a hard-deleted issue's
-- events from binding to a later SQLite row that would otherwise reuse the id.
CREATE TABLE issue_activity_id_reservations (
    issue_id INTEGER PRIMARY KEY CHECK (issue_id > 0)
);

INSERT OR IGNORE INTO issue_activity_id_reservations
    SELECT id FROM issues;
INSERT OR IGNORE INTO issue_activity_id_reservations
    SELECT target_id FROM activity
    WHERE target_kind = 'issue' AND target_id > 0;

CREATE TABLE issue_activity_id_sequence (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    next_id   INTEGER NOT NULL CHECK (next_id > 0)
);

INSERT INTO issue_activity_id_sequence (singleton, next_id)
    SELECT 1, COALESCE(MAX(issue_id), 0) + 1
    FROM issue_activity_id_reservations;

CREATE TRIGGER reserve_issue_activity_id
AFTER INSERT ON issues
BEGIN
    INSERT INTO issue_activity_id_reservations VALUES (NEW.id);
    UPDATE issue_activity_id_sequence
       SET next_id = MAX(next_id, NEW.id + 1)
     WHERE singleton = 1;
END;

CREATE TRIGGER reserve_issue_activity_event_id
AFTER INSERT ON activity
WHEN NEW.target_kind = 'issue' AND NEW.target_id > 0
BEGIN
    INSERT OR IGNORE INTO issue_activity_id_reservations VALUES (NEW.target_id);
    UPDATE issue_activity_id_sequence
       SET next_id = MAX(next_id, NEW.target_id + 1)
     WHERE singleton = 1;
END;

CREATE TRIGGER issue_activity_id_reservations_immutable_update
BEFORE UPDATE ON issue_activity_id_reservations
BEGIN
    SELECT RAISE(ABORT, 'issue activity ids are immutable');
END;

CREATE TRIGGER issue_activity_id_reservations_immutable_delete
BEFORE DELETE ON issue_activity_id_reservations
BEGIN
    SELECT RAISE(ABORT, 'issue activity ids are immutable');
END;

CREATE TABLE issue_lifecycle_facts (
    event_id                 INTEGER PRIMARY KEY REFERENCES activity(id),
    issue_id                 INTEGER NOT NULL CHECK (issue_id > 0),
    previous_event_id        INTEGER REFERENCES issue_lifecycle_facts(event_id),
    fact_version             INTEGER NOT NULL DEFAULT 1 CHECK (fact_version = 1),
    event_kind               TEXT NOT NULL
                                 CHECK (event_kind IN ('created', 'status_transition')),
    before_status            TEXT,
    before_category          TEXT CHECK (
                                 before_category IS NULL OR
                                 before_category IN ('todo', 'doing', 'done')
                             ),
    after_status             TEXT NOT NULL CHECK (length(after_status) > 0),
    after_category           TEXT NOT NULL
                                 CHECK (after_category IN ('todo', 'doing', 'done')),
    before_project_scope_key TEXT,
    after_project_scope_key  TEXT,
    actor_kind               TEXT NOT NULL
                                 CHECK (actor_kind IN ('human', 'agent')),
    CHECK (
        (event_kind = 'created' AND
         previous_event_id IS NULL AND
         before_status IS NULL AND before_category IS NULL AND
         before_project_scope_key IS NULL)
        OR
        (event_kind = 'status_transition' AND
         before_status IS NOT NULL AND before_category IS NOT NULL)
    ),
    CHECK (
        before_project_scope_key IS NULL OR
        length(before_project_scope_key) = 32
    ),
    CHECK (
        after_project_scope_key IS NULL OR
        length(after_project_scope_key) = 32
    )
);

-- Tie every fact to the exact native, unrestricted activity event it describes.
-- This prevents imports and ad-hoc rows from acquiring trusted metric semantics.
CREATE TRIGGER issue_lifecycle_fact_event_required
BEFORE INSERT ON issue_lifecycle_facts
WHEN NOT EXISTS (
    SELECT 1
      FROM activity AS a
     WHERE a.id = NEW.event_id
       AND a.target_kind = 'issue'
       AND a.target_id = NEW.issue_id
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
       AND (
           (NEW.event_kind = 'created' AND a.verb = 'created')
           OR
           (NEW.event_kind = 'status_transition' AND a.verb = 'changed_status')
       )
)
BEGIN
    SELECT RAISE(ABORT, 'matching native issue activity event required');
END;

CREATE TRIGGER issue_lifecycle_fact_predecessor_required
BEFORE INSERT ON issue_lifecycle_facts
WHEN NEW.previous_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
      FROM issue_lifecycle_facts AS previous
     WHERE previous.event_id = NEW.previous_event_id
       AND previous.issue_id = NEW.issue_id
       AND previous.event_id < NEW.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'matching earlier lifecycle predecessor required');
END;


CREATE TRIGGER issue_lifecycle_facts_immutable_update
BEFORE UPDATE ON issue_lifecycle_facts
BEGIN
    SELECT RAISE(ABORT, 'issue lifecycle facts are append-only');
END;

CREATE TRIGGER issue_lifecycle_facts_immutable_delete
BEFORE DELETE ON issue_lifecycle_facts
BEGIN
    SELECT RAISE(ABORT, 'issue lifecycle facts are append-only');
END;

CREATE UNIQUE INDEX idx_issue_lifecycle_one_creation
    ON issue_lifecycle_facts (issue_id)
    WHERE event_kind = 'created';

CREATE INDEX idx_issue_lifecycle_issue_event
    ON issue_lifecycle_facts (issue_id, event_id);

CREATE UNIQUE INDEX idx_issue_lifecycle_one_root
    ON issue_lifecycle_facts (issue_id)
    WHERE previous_event_id IS NULL;

CREATE UNIQUE INDEX idx_issue_lifecycle_one_successor
    ON issue_lifecycle_facts (previous_event_id)
    WHERE previous_event_id IS NOT NULL;

-- The metrics evidence query fixes target kind and verbs, then walks a bounded
-- UTC range in event order. Before this index SQLite selected idx_activity_target
-- and walked all issue activity, sorting through a temporary B-tree.
CREATE INDEX idx_activity_issue_lifecycle_window
    ON activity (target_kind, created_at, id)
    WHERE target_kind = 'issue'
      AND verb IN ('created', 'changed_status');
