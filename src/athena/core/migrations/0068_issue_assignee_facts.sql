-- 0068_issue_assignee_facts: structured before/after for issue assignment, so
-- an assignment can be reversed from evidence rather than inferred from prose.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0069_*.sql.
--
-- The `assigned` activity event records only the NEW assignee's display name --
-- names are not unique, "unassigned" records nothing at all, and a re-assign
-- (U1 -> U2) is indistinguishable from a first assign (NULL -> U2). Undo by
-- compensation therefore had nothing trustworthy to restore, and driving a
-- mutation by parsing event prose is exactly what UNDO.md forbids. This is the
-- assignee twin of 0055's issue_lifecycle_facts: the typed before/after,
-- snapshotted in the same transaction as the event, keyed 1:1 to it, immutable.
--
-- Deliberately NOT here, mirroring 0055's restraint:
--   * no backfill -- a pre-0068 assignment event has no fact, and undo refuses it
--     honestly instead of guessing;
--   * no predecessor chain -- that is 0055 metrics machinery; undo reads one row
--     by event id and needs no ordering;
--   * no project scope keys -- assignment is not project-scoped the way statuses
--     are (a project move remaps an issue's status, never its assignee).
--
-- The assignee columns keep a plain REFERENCES users(id), exactly like
-- activity.actor_id (0017): Athena never hard-deletes a user (offboarding
-- revokes credentials and leaves the row), so the reference always resolves --
-- and if user deletion is ever built, these rows make the dangling references a
-- loud failure instead of a silent orphan.
CREATE TABLE issue_assignee_facts (
    event_id           INTEGER PRIMARY KEY REFERENCES activity(id),
    issue_id           INTEGER NOT NULL CHECK (issue_id > 0),
    fact_version       INTEGER NOT NULL DEFAULT 1 CHECK (fact_version = 1),
    -- NULL means "was/became unassigned". IS NOT is null-safe, so the CHECK
    -- refuses a no-op fact: the recorder only fires on a real change.
    before_assignee_id INTEGER REFERENCES users(id)
                           CHECK (before_assignee_id IS NULL OR before_assignee_id > 0),
    after_assignee_id  INTEGER REFERENCES users(id)
                           CHECK (after_assignee_id IS NULL OR after_assignee_id > 0),
    CHECK (before_assignee_id IS NOT after_assignee_id)
);

-- Tie every fact to the exact native, fully-resolved activity event it
-- describes, with the verb agreeing with the direction: an 'assigned' event
-- gains an assignee, an 'unassigned' event clears one. This keeps imports and
-- ad-hoc rows from acquiring trusted undo semantics, exactly as 0055 does for
-- lifecycle facts.
CREATE TRIGGER issue_assignee_fact_event_required
BEFORE INSERT ON issue_assignee_facts
WHEN NOT EXISTS (
    SELECT 1
      FROM activity AS a
     WHERE a.id = NEW.event_id
       AND a.target_kind = 'issue'
       AND a.target_id = NEW.issue_id
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
       AND (
           (NEW.after_assignee_id IS NOT NULL AND a.verb = 'assigned')
           OR
           (NEW.after_assignee_id IS NULL AND a.verb = 'unassigned')
       )
)
BEGIN
    SELECT RAISE(ABORT, 'matching native issue assignment event required');
END;

CREATE TRIGGER issue_assignee_facts_immutable_update
BEFORE UPDATE ON issue_assignee_facts
BEGIN
    SELECT RAISE(ABORT, 'issue assignee facts are append-only');
END;

CREATE TRIGGER issue_assignee_facts_immutable_delete
BEFORE DELETE ON issue_assignee_facts
BEGIN
    SELECT RAISE(ABORT, 'issue assignee facts are append-only');
END;
