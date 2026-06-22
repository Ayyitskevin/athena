-- 0002_api_tokens: authenticated API tokens for users (humans and agents).
-- FORWARD-ONLY: once applied anywhere, never edit this file; add 0003_*.sql to change.

-- A bearer token proves a caller IS a given user, unlike the X-Athena-Actor
-- header which only claims an id. We store the SHA-256 hash, never the raw
-- token: a leak of this table yields no usable credential.
CREATE TABLE api_tokens (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    name         TEXT NOT NULL,                       -- human label, e.g. "grok-ci"
    token_hash   TEXT NOT NULL UNIQUE,                -- sha256 hex of the raw token
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT,                                -- stamped on each successful auth (audit)
    revoked_at   TEXT                                 -- set to disable; resolve ignores revoked rows
);

-- Auth hashes the presented token and looks it up here on every request.
CREATE INDEX idx_api_tokens_hash ON api_tokens (token_hash);
