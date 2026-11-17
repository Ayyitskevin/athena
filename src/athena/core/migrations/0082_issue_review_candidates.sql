-- Exact-version review tracking. Completion activity owns reports and attribution;
-- these immutable operational rows bind an explicit identity to that evidence.
-- Selective portability v1 excludes these rows. No historical backfill.
-- FORWARD-ONLY: never edit after application; add a subsequent migration.

CREATE TABLE issue_review_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    completion_event_id INTEGER NOT NULL UNIQUE REFERENCES activity(id),
    kind TEXT NOT NULL CHECK (kind IN ('git', 'artifact')),
    source TEXT NOT NULL CHECK (
        length(source) BETWEEN 1 AND 200 AND
        source = trim(source, ' ' || char(9) || char(10) || char(13))
    ),
    revision TEXT NOT NULL CHECK (
        revision NOT GLOB '*[^0-9a-f]*' AND
        (length(revision) = 64 OR (kind = 'git' AND length(revision) = 40))
    ),
    previous_candidate_id INTEGER REFERENCES issue_review_candidates(id)
);

CREATE INDEX idx_issue_review_candidates_latest
    ON issue_review_candidates(issue_id, id DESC);

CREATE TABLE issue_candidate_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES issue_review_candidates(id),
    review_issue_id INTEGER NOT NULL REFERENCES issues(id),
    completion_event_id INTEGER NOT NULL UNIQUE REFERENCES activity(id)
);

CREATE INDEX idx_issue_candidate_reviews_latest
    ON issue_candidate_reviews(candidate_id, completion_event_id DESC);

CREATE TRIGGER issue_review_candidate_native_event
BEFORE INSERT ON issue_review_candidates
WHEN NOT EXISTS (
    SELECT 1 FROM activity a
    WHERE a.id = NEW.completion_event_id
      AND a.target_kind = 'issue' AND a.target_id = NEW.issue_id
      AND a.verb = 'claim_completed' AND a.actor_id IS NOT NULL
      AND a.imported_at IS NULL AND a.visibility_restricted = 0
) OR NEW.previous_candidate_id IS NOT (
    SELECT MAX(id) FROM issue_review_candidates WHERE issue_id = NEW.issue_id
)
BEGIN
    SELECT RAISE(ABORT, 'matching native completion and latest predecessor required');
END;

CREATE TRIGGER issue_candidate_review_native_event
BEFORE INSERT ON issue_candidate_reviews
WHEN NOT EXISTS (
    SELECT 1 FROM issue_review_candidates c
    JOIN activity source ON source.id = c.completion_event_id
    JOIN activity a ON a.id = NEW.completion_event_id
    WHERE c.id = NEW.candidate_id AND c.issue_id <> NEW.review_issue_id
      AND source.actor_id <> a.actor_id
      AND source.imported_at IS NULL AND source.visibility_restricted = 0
      AND a.target_kind = 'issue' AND a.target_id = NEW.review_issue_id
      AND a.verb = 'claim_completed' AND a.actor_id IS NOT NULL
      AND a.imported_at IS NULL AND a.visibility_restricted = 0
)
BEGIN
    SELECT RAISE(ABORT, 'distinct native candidate review completion required');
END;

CREATE TRIGGER issue_review_candidates_no_update
BEFORE UPDATE ON issue_review_candidates
BEGIN
    SELECT RAISE(ABORT, 'review candidates are immutable');
END;

CREATE TRIGGER issue_review_candidates_no_delete
BEFORE DELETE ON issue_review_candidates
BEGIN
    SELECT RAISE(ABORT, 'review candidates are durable');
END;

CREATE TRIGGER issue_candidate_reviews_no_update
BEFORE UPDATE ON issue_candidate_reviews
BEGIN
    SELECT RAISE(ABORT, 'candidate reviews are immutable');
END;

CREATE TRIGGER issue_candidate_reviews_no_delete
BEFORE DELETE ON issue_candidate_reviews
BEGIN
    SELECT RAISE(ABORT, 'candidate reviews are durable');
END;

-- The lease has already been released when these rows are inserted. Active-own
-- generation authority is established by the command under its writer lock;
-- these triggers pin the completed evidence, not a fictitious post-release lease.
CREATE TRIGGER issue_review_activity_no_update
BEFORE UPDATE ON activity
WHEN EXISTS (SELECT 1 FROM issue_review_candidates c WHERE c.completion_event_id = OLD.id)
  OR EXISTS (SELECT 1 FROM issue_candidate_reviews r WHERE r.completion_event_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'candidate completion activity is immutable');
END;

CREATE TRIGGER issue_review_activity_no_delete
BEFORE DELETE ON activity
WHEN EXISTS (SELECT 1 FROM issue_review_candidates c WHERE c.completion_event_id = OLD.id)
  OR EXISTS (SELECT 1 FROM issue_candidate_reviews r WHERE r.completion_event_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'candidate completion activity is immutable');
END;
