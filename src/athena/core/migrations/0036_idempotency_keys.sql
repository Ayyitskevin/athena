-- 0036_idempotency_keys: replay the first response to a repeated write.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0037_*.sql.
--
-- Agent-native safety: an agent whose POST times out on a network blip retries it,
-- and without this would double-create (two issues, two comments). When a client
-- sends an `Idempotency-Key` header, the first successful response is stored here,
-- keyed by (key, identity), and any later request with the same key REPLAYS that
-- stored response instead of running the write again.
--
-- identity scopes the key to the caller (a token hash, or "actor:<id>"), so two
-- callers reusing the same key string never collide and never read each other's
-- responses. The composite primary key makes (key, identity) unique and is the
-- dedupe index; INSERT OR IGNORE makes concurrent first-writes safe to store.

CREATE TABLE idempotency_keys (
    idempotency_key TEXT NOT NULL,
    identity        TEXT NOT NULL,
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    content_type    TEXT,
    response_body   BLOB NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (idempotency_key, identity)
);
