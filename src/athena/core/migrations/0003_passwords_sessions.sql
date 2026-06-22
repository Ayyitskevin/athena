-- 0003_passwords_sessions: browser login — per-user passwords + server-side sessions.
-- FORWARD-ONLY: once applied anywhere, never edit this file; add 0004_*.sql to change.

-- A password lets a human log in through the browser. Nullable: agents (and any
-- user created without one) have no password and simply can't browser-login —
-- they still act via API tokens. Stored as a salted pbkdf2 hash, never plaintext.
ALTER TABLE users ADD COLUMN password_hash TEXT;

-- A browser session is a server-side row keyed by a random cookie value. Like
-- api_tokens, only the HASH of that value is stored, so the table holds nothing
-- replayable. Logout deletes the row; expired rows stop authenticating.
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    session_hash TEXT NOT NULL UNIQUE,                -- sha256 hex of the cookie value
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL                        -- past this instant, auth fails
);

CREATE INDEX idx_sessions_hash ON sessions (session_hash);
