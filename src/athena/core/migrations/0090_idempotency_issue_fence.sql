-- 0090_idempotency_issue_fence: an issue's assignment or creator changing fences
-- only the receipts that depend on that issue, and a fenced marker expires when
-- its receipt would have (F6, decision D17).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add a new one.
--
-- The argument for what may be scoped is in core/idempotency.py. In short: an
-- issue's assignee, creator and contributors are never read by a visibility rule,
-- only by the write gates of that same issue, so a receipt whose request named
-- the issue in its path, and whose route consults no other issue's assignee,
-- creator or contributors, depends on that issue's alone. Placement
-- (project_id) decides visibility and is embedded in other issues' answers, so it
-- stays global.
--
-- THE COUNTERS, and why the old one keeps moving. 0043's `revision` still moves
-- on EVERY act it moved before, and on the acts added below. A process still
-- running the old code compares only `revision`, so it keeps failing closed on an
-- assignment exactly as before, whatever order the processes are restarted in.
-- The new code reads two more counters:
--   * issue_authority_revision (workspace-wide) moves, together with `revision`,
--     on every per-issue act: an issue's assignee or creator changing, or one of
--     its contributors removed. `revision - issue_authority_revision` therefore
--     counts only the workspace-wide acts: the new code's global component.
--   * idempotency_issue_authority.revision (one per issue) moves on that issue's
--     per-issue acts.
--
-- COST. The rebuild copies every receipt. Measured with ~300-byte responses: at
-- 200,000 receipts it takes 1.4 s, the database file grows from 77 MB to 154 MB
-- and the WAL to 154 MB, and SQLite's write lock is held throughout, so every
-- other writer waits; at 1,000,000 receipts it takes 7.4 s. Writers give up
-- after the 5-second busy timeout, which this passes at roughly 700,000 receipts
-- (about TTL x keyed writes per second). Above that, migrate with the service
-- stopped; see OPERATIONS.md.
--
-- Nothing here is looked up by this file's number.

-- 1. The workspace-wide counter of per-issue acts.
ALTER TABLE idempotency_authorization_state
    ADD COLUMN issue_authority_revision INTEGER NOT NULL DEFAULT 0
    CHECK (issue_authority_revision >= 0);

-- 2. One counter per issue that has had a per-issue act. A missing row reads as 0.
--    No foreign key and no delete: issues are archived, never deleted, and a
--    counter that could restart at 0 could match a stale receipt.
CREATE TABLE idempotency_issue_authority (
    issue_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 0)
);

-- 3. Receipts learn their scope. A fenced marker ('indeterminate' with
--    failure_code 'authorization_changed') may now carry expires_at: the instant
--    its receipt would have expired. Every other indeterminate row still never
--    expires, because its write may or may not have been applied.
--    Old code inserts rows naming only its own columns; the defaults below make
--    such a row 'workspace', which the new code compares on `revision` alone.
ALTER TABLE idempotency_keys RENAME TO idempotency_keys_0043;

CREATE TABLE idempotency_keys (
    idempotency_key       TEXT NOT NULL,
    identity              TEXT NOT NULL,
    request_fingerprint   TEXT NOT NULL,
    -- `revision` at the claim: every act. Compared by old code, and by new code
    -- for scope 'workspace'.
    authorization_revision INTEGER NOT NULL CHECK (authorization_revision >= 0),
    -- 'workspace': fenced by every act (the old rule);
    -- 'issue': fenced by the workspace-wide acts and scope_issue_id's per-issue acts;
    -- 'none': fenced by the workspace-wide acts only (POST /issues).
    authority_scope     TEXT NOT NULL DEFAULT 'workspace'
        CHECK (authority_scope IN ('workspace', 'issue', 'none')),
    scope_issue_id      INTEGER,
    -- `revision - issue_authority_revision` at the claim (scopes 'issue', 'none').
    access_revision     INTEGER NOT NULL DEFAULT 0 CHECK (access_revision >= 0),
    -- scope_issue_id's own counter at the claim (scope 'issue'), else 0.
    issue_authority_revision INTEGER NOT NULL DEFAULT 0
        CHECK (issue_authority_revision >= 0),
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
    CHECK ((authority_scope = 'issue') = (scope_issue_id IS NOT NULL)),
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
            AND (expires_at IS NULL OR failure_code = 'authorization_changed'))
    )
);

-- Existing rows keep the old rule: scope 'workspace'. Markers fenced before this
-- migration keep no expiry: their receipt's expiry was discarded with the
-- response, and guessing it from a setting that may have changed could end the
-- fence before the receipt would have expired.
INSERT INTO idempotency_keys (
    idempotency_key, identity, request_fingerprint, authorization_revision,
    authority_scope, scope_issue_id, access_revision, issue_authority_revision,
    method, path, state, owner_token, lease_expires_at, status_code, content_type,
    response_headers, response_body, failure_code, created_at, updated_at, expires_at
)
SELECT
    idempotency_key, identity, request_fingerprint, authorization_revision,
    'workspace', NULL, 0, 0,
    method, path, state, owner_token, lease_expires_at, status_code, content_type,
    response_headers, response_body, failure_code, created_at, updated_at, expires_at
FROM idempotency_keys_0043;

DROP TABLE idempotency_keys_0043;

-- Completed receipts and fenced markers expire; nothing else carries expires_at.
CREATE INDEX idx_idempotency_expiry
    ON idempotency_keys (expires_at)
    WHERE expires_at IS NOT NULL;

-- 4. Split 0043's issue trigger. Placement and identity are workspace-wide acts.
DROP TRIGGER idempotency_authz_issue_authority_changed;

CREATE TRIGGER idempotency_authz_issue_placement_changed
AFTER UPDATE OF id, project_id ON issues
WHEN NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

-- The per-issue acts. Each moves `revision` (for old code and for the
-- 'workspace' scope), the workspace-wide issue counter (so the new code's
-- global component does not move) and the issue's own counter.
CREATE TRIGGER idempotency_issue_authority_changed
AFTER UPDATE OF created_by, assignee_id ON issues
WHEN NEW.created_by IS NOT OLD.created_by OR NEW.assignee_id IS NOT OLD.assignee_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1,
           issue_authority_revision = issue_authority_revision + 1
     WHERE singleton = 1;
    INSERT OR IGNORE INTO idempotency_issue_authority (issue_id, revision)
        VALUES (NEW.id, 0);
    UPDATE idempotency_issue_authority
       SET revision = revision + 1 WHERE issue_id = NEW.id;
END;

-- A contributor removed (DELETE /contributors, a decline) or moved narrows that
-- account's write authority on the issue it left (issues.can_act_on, the claimant
-- and reviewer gates). Before F6 this moved no counter at all.
CREATE TRIGGER idempotency_issue_contributor_removed
AFTER DELETE ON issue_contributors
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1,
           issue_authority_revision = issue_authority_revision + 1
     WHERE singleton = 1;
    INSERT OR IGNORE INTO idempotency_issue_authority (issue_id, revision)
        VALUES (OLD.issue_id, 0);
    UPDATE idempotency_issue_authority
       SET revision = revision + 1 WHERE issue_id = OLD.issue_id;
END;

CREATE TRIGGER idempotency_issue_contributor_moved
AFTER UPDATE OF issue_id, user_id ON issue_contributors
WHEN NEW.issue_id IS NOT OLD.issue_id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1,
           issue_authority_revision = issue_authority_revision + 1
     WHERE singleton = 1;
    INSERT OR IGNORE INTO idempotency_issue_authority (issue_id, revision)
        VALUES (OLD.issue_id, 0);
    UPDATE idempotency_issue_authority
       SET revision = revision + 1 WHERE issue_id = OLD.issue_id;
END;

-- 5. Workspace-wide policy acts that refuse a write which was allowed. Before F6
--    none moved a counter.
-- An account marked an agent meets the blocked-close policy (an agent may not
-- close a blocked issue, even with the override).
CREATE TRIGGER idempotency_authz_user_is_agent
AFTER UPDATE OF is_agent ON users
WHEN NEW.is_agent IS NOT OLD.is_agent
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

-- A project's blocked-close policy turned on or off.
CREATE TRIGGER idempotency_authz_project_close_policy
AFTER UPDATE OF block_agent_closes_when_blocked ON projects
WHEN NEW.block_agent_closes_when_blocked IS NOT OLD.block_agent_closes_when_blocked
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

-- An approval gate added (or removed, or rewritten) for an account's action kind.
CREATE TRIGGER idempotency_authz_approval_policy_added
AFTER INSERT ON agent_approval_policies
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_approval_policy_removed
AFTER DELETE ON agent_approval_policies
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_approval_policy_changed
AFTER UPDATE OF user_id, action_kind ON agent_approval_policies
WHEN NEW.user_id IS NOT OLD.user_id OR NEW.action_kind IS NOT OLD.action_kind
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

-- An action budget set or its limit or window changed by the operator. Spending
-- (action_used, window_started_at) moves nothing: every metered write spends, and
-- a replay applies nothing and is not metered. Removing a budget only widens.
CREATE TRIGGER idempotency_authz_budget_set
AFTER INSERT ON agent_budgets
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

CREATE TRIGGER idempotency_authz_budget_changed
AFTER UPDATE OF user_id, action_limit, window ON agent_budgets
WHEN NEW.user_id IS NOT OLD.user_id
    OR NEW.action_limit IS NOT OLD.action_limit
    OR NEW.window IS NOT OLD.window
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;
