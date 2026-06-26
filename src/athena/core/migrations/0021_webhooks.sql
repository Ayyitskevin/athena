-- 0021_webhooks: outbound webhooks — push the event log to registered endpoints.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0022.
--
-- The pull event feed (GET /events, migration-free) lets a consumer drain the
-- audit trail on its own schedule. A webhook is the push twin: Athena POSTs each
-- new event to a registered URL, HMAC-signed so the receiver can trust it. Like
-- the pull feed, delivery is CURSOR-based over the existing `activity` log rather
-- than a separate per-delivery outbox: each webhook remembers the last activity id
-- it received, and a background loop advances that cursor as it delivers. The
-- audit log stays the single source of truth; this table only tracks where each
-- subscriber is up to.
CREATE TABLE webhooks (
    id              INTEGER PRIMARY KEY,
    url             TEXT NOT NULL,                       -- where to POST (http/https, SSRF-checked at the boundary)
    secret          TEXT NOT NULL,                       -- HMAC-SHA256 signing secret; shared with the receiver, shown once
    event_kind      TEXT,                                -- optional filter: only this target_kind (NULL = every event)
    active          INTEGER NOT NULL DEFAULT 1,          -- 0 disables delivery without deleting the row
    -- The high-water mark: the largest activity.id already delivered. A new webhook
    -- starts at the current tip (set by the API on create), so it receives only
    -- events from registration onward, never the whole history.
    cursor          INTEGER NOT NULL DEFAULT 0,
    -- Delivery health, for backoff and operator visibility. failure_count is the
    -- run of consecutive failures (reset to 0 on any success); next_attempt_at is
    -- the backoff gate — the loop skips a webhook while now < next_attempt_at.
    -- Timestamps are 'YYYY-MM-DD HH:MM:SS' (UTC), so a lexicographic compare is
    -- also a chronological one.
    failure_count   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    last_success_at TEXT,
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The delivery loop's hot query is "active webhooks due for an attempt".
CREATE INDEX idx_webhooks_active ON webhooks (active, next_attempt_at);
