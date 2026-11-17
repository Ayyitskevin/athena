-- Exact-version requests are native operational facts. No selective-import backfill.
CREATE TABLE issue_review_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES issue_review_candidates(id),
    review_issue_id INTEGER NOT NULL REFERENCES issues(id),
    reviewer_id INTEGER NOT NULL REFERENCES users(id),
    request_event_id INTEGER NOT NULL UNIQUE REFERENCES activity(id),
    UNIQUE(review_issue_id, candidate_id)
);
CREATE INDEX idx_review_requests_issue ON issue_review_requests(review_issue_id, id DESC);
CREATE INDEX idx_review_requests_candidate ON issue_review_requests(candidate_id, id DESC);
CREATE INDEX idx_review_requests_reviewer ON issue_review_requests(reviewer_id, id DESC);

CREATE TRIGGER issue_review_request_native_event
BEFORE INSERT ON issue_review_requests
WHEN NOT EXISTS (
    SELECT 1 FROM activity a
    JOIN issue_review_candidates c ON c.id = NEW.candidate_id
    JOIN activity source ON source.id = c.completion_event_id
    WHERE a.id = NEW.request_event_id AND a.id > source.id
      AND a.verb = 'review_requested' AND a.target_kind = 'issue'
      AND a.target_id = NEW.review_issue_id AND a.actor_id IS NOT NULL
      AND a.imported_at IS NULL AND a.visibility_restricted = 0 AND a.detail = ''
      AND c.issue_id <> NEW.review_issue_id AND source.actor_id <> NEW.reviewer_id
) OR EXISTS (
    -- REPLACE can otherwise bypass DELETE triggers with default SQLite settings.
    SELECT 1 FROM issue_review_requests
    WHERE id = NEW.id OR request_event_id = NEW.request_event_id
       OR (review_issue_id = NEW.review_issue_id AND candidate_id = NEW.candidate_id)
)
BEGIN
    SELECT RAISE(ABORT, 'a new matching native review request is required');
END;

CREATE TRIGGER issue_review_requests_no_update BEFORE UPDATE ON issue_review_requests
BEGIN
    SELECT RAISE(ABORT, 'review requests are immutable');
END;
CREATE TRIGGER issue_review_requests_no_delete BEFORE DELETE ON issue_review_requests
BEGIN
    SELECT RAISE(ABORT, 'review requests are durable');
END;
CREATE TRIGGER issue_review_request_event_no_update BEFORE UPDATE ON activity
WHEN EXISTS (SELECT 1 FROM issue_review_requests WHERE request_event_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'review request activity is immutable');
END;
CREATE TRIGGER issue_review_request_event_no_delete BEFORE DELETE ON activity
WHEN EXISTS (SELECT 1 FROM issue_review_requests WHERE request_event_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'review request activity is durable');
END;
CREATE TRIGGER issue_review_request_event_no_replace BEFORE INSERT ON activity
WHEN EXISTS (SELECT 1 FROM issue_review_requests WHERE request_event_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'review request activity is immutable');
END;
