-- 0043_durable_idempotency: turn the sequential response cache into a durable
-- single-flight state machine.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0044_*.sql.
--
-- An executing row is a durable ownership claim.  Its nonce fences finalization,
-- so only the worker that inserted it may publish the result.  Completed rows
-- contain the bounded response and expire normally.  Indeterminate rows do not
-- expire automatically: a route may have committed before its response could be
-- recorded, and silently retrying that ambiguous write could duplicate it.
--
-- V1 rows have no request-body fingerprint.  They cannot be safely matched to a
-- retry, and some may contain one-time secrets from the formerly global cache.
-- Preserve their keys fail-closed as indeterminate, but deliberately discard the
-- cached response bytes.

ALTER TABLE idempotency_keys RENAME TO idempotency_keys_v1;

-- A global, monotonic security epoch fences response bodies after any change that
-- may revoke access. It is read inside the same BEGIN IMMEDIATE claim transaction
-- as the receipt, so a membership/visibility write cannot race the replay check.
CREATE TABLE idempotency_authorization_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision  INTEGER NOT NULL CHECK (revision >= 0)
);
INSERT INTO idempotency_authorization_state VALUES (1, 0);

CREATE TABLE idempotency_keys (
    idempotency_key       TEXT NOT NULL,
    identity              TEXT NOT NULL,
    request_fingerprint   TEXT NOT NULL,
    authorization_revision INTEGER NOT NULL CHECK (authorization_revision >= 0),
    method              TEXT NOT NULL,
    path                TEXT NOT NULL,
    state               TEXT NOT NULL
        CHECK (state IN ('executing', 'completed', 'indeterminate')),
    owner_token         TEXT,
    lease_expires_at    INTEGER,
    status_code         INTEGER,
    content_type        TEXT,
    response_headers    TEXT,
    response_body       BLOB,
    failure_code        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          INTEGER,
    PRIMARY KEY (idempotency_key, identity),
    CHECK (
        (state = 'executing'
            AND owner_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND status_code IS NULL
            AND response_headers IS NULL
            AND response_body IS NULL
            AND failure_code IS NULL
            AND expires_at IS NULL)
        OR
        (state = 'completed'
            AND owner_token IS NULL
            AND lease_expires_at IS NULL
            AND status_code IS NOT NULL
            AND status_code BETWEEN 200 AND 299
            AND response_headers IS NOT NULL
            AND response_body IS NOT NULL
            AND failure_code IS NULL
            AND expires_at IS NOT NULL)
        OR
        (state = 'indeterminate'
            AND owner_token IS NULL
            AND lease_expires_at IS NULL
            AND status_code IS NULL
            AND response_headers IS NULL
            AND response_body IS NULL
            AND failure_code IS NOT NULL
            AND expires_at IS NULL)
    )
);

INSERT INTO idempotency_keys (
    idempotency_key,
    identity,
    request_fingerprint,
    authorization_revision,
    method,
    path,
    state,
    failure_code,
    created_at,
    updated_at
)
SELECT
    idempotency_key,
    identity,
    'legacy-fingerprint-unavailable',
    (SELECT revision FROM idempotency_authorization_state WHERE singleton = 1),
    method,
    path,
    'indeterminate',
    'legacy_fingerprint_unavailable',
    created_at,
    created_at
FROM idempotency_keys_v1;

DROP TABLE idempotency_keys_v1;

CREATE INDEX idx_idempotency_completed_expiry
    ON idempotency_keys (expires_at)
    WHERE state = 'completed';

-- Invalidate completed response bodies whenever current authorization may be
-- narrower than it was at claim time. Expansions do not need fencing; subsequent
-- route authorization handles them normally. These are database triggers so REST,
-- web, imports, and direct command composition cannot forget the invariant.
CREATE TRIGGER idempotency_authz_user_identity_or_role
AFTER UPDATE OF id, role ON users
WHEN NEW.id IS NOT OLD.id OR NEW.role IS NOT OLD.role
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_token_identity_principal_or_scopes
AFTER UPDATE OF user_id, token_hash, scopes ON api_tokens
WHEN NEW.user_id IS NOT OLD.user_id
    OR NEW.token_hash IS NOT OLD.token_hash
    OR NEW.scopes IS NOT OLD.scopes
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;



CREATE TRIGGER idempotency_authz_user_deleted
AFTER DELETE ON users
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_token_deleted
AFTER DELETE ON api_tokens
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_project_visibility_or_owner
AFTER UPDATE OF visibility, created_by ON projects
WHEN NEW.visibility IS NOT OLD.visibility OR NEW.created_by IS NOT OLD.created_by
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_project_deleted
AFTER DELETE ON projects
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_project_member_removed
AFTER DELETE ON project_members
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_project_member_reassigned
AFTER UPDATE OF project_id, user_id ON project_members
WHEN NEW.project_id IS NOT OLD.project_id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_issue_authority_changed
AFTER UPDATE OF project_id, created_by, assignee_id ON issues
WHEN NEW.project_id IS NOT OLD.project_id
    OR NEW.created_by IS NOT OLD.created_by
    OR NEW.assignee_id IS NOT OLD.assignee_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_space_visibility_or_owner
AFTER UPDATE OF visibility, created_by ON spaces
WHEN NEW.visibility IS NOT OLD.visibility OR NEW.created_by IS NOT OLD.created_by
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_space_deleted
AFTER DELETE ON spaces
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_space_member_removed
AFTER DELETE ON space_members
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_space_member_reassigned
AFTER UPDATE OF space_id, user_id ON space_members
WHEN NEW.space_id IS NOT OLD.space_id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_page_moved
AFTER UPDATE OF space_id ON pages
WHEN NEW.space_id IS NOT OLD.space_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;


CREATE TRIGGER idempotency_authz_sprint_project_changed
AFTER UPDATE OF project_id ON sprints
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_comment_authority_changed
AFTER UPDATE OF issue_id, author_id ON comments
WHEN NEW.issue_id IS NOT OLD.issue_id OR NEW.author_id IS NOT OLD.author_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_page_comment_authority_changed
AFTER UPDATE OF page_id, author_id ON page_comments
WHEN NEW.page_id IS NOT OLD.page_id OR NEW.author_id IS NOT OLD.author_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_attachment_authority_changed
AFTER UPDATE OF target_kind, target_id, uploaded_by ON attachments
WHEN NEW.target_kind IS NOT OLD.target_kind
    OR NEW.target_id IS NOT OLD.target_id
    OR NEW.uploaded_by IS NOT OLD.uploaded_by
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_filter_owner_changed
AFTER UPDATE OF owner_id ON saved_filters
WHEN NEW.owner_id IS NOT OLD.owner_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_notification_owner_changed
AFTER UPDATE OF user_id, event_id ON notifications
WHEN NEW.user_id IS NOT OLD.user_id OR NEW.event_id IS NOT OLD.event_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_watch_owner_or_target_changed
AFTER UPDATE OF user_id, target_kind, target_id ON watches
WHEN NEW.user_id IS NOT OLD.user_id
    OR NEW.target_kind IS NOT OLD.target_kind
    OR NEW.target_id IS NOT OLD.target_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;
