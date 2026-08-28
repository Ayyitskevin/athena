-- 0080_notification_priority: per-watch priority, mute, and digest controls.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0081_*.sql.
--
-- WHY: a solo operator needs to shape the firehose of notifications without
-- creating a second event authority. These rows are PREFERENCES over the
-- existing watch/subscription surface; the inbox and activity log remain the
-- single source of truth. Projections read watches + preferences + issue
-- priority (when the watched target is an issue) and compute a resolved
-- priority, mute state, and digest bucket at read time.

CREATE TABLE watch_preferences (
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    target_kind             TEXT    NOT NULL
                                    CHECK (target_kind IN ('issue', 'page', 'space')),
    target_id               INTEGER NOT NULL,
    -- Explicit priority override for this watch. NULL means "fall back to the
    -- target's own priority (issue.priority) or the default normal priority."
    priority                TEXT
                                    CHECK (
                                        priority IS NULL OR
                                        priority IN ('low', 'medium', 'high', 'urgent')
                                    ),
    -- ISO-8601 datetime. While now() is before mute_until, every notification
    -- for this watch is suppressed (fail closed: an unparseable value is
    -- treated as not muted, so a stale/broken setting never silently drops mail).
    mute_until              TEXT,
    -- If set, notifications for this watch are grouped into digest buckets of
    -- this many minutes. NULL means "deliver immediately / no digest."
    digest_window_minutes   INTEGER
                                    CHECK (
                                        digest_window_minutes IS NULL OR
                                        digest_window_minutes BETWEEN 1 AND 10080
                                    ),
    created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, target_kind, target_id),
    FOREIGN KEY (user_id, target_kind, target_id)
        REFERENCES watches (user_id, target_kind, target_id)
        ON DELETE CASCADE
);

-- The primary key already covers reads for one user. The reverse index supports
-- target lifecycle/reconciliation queries without duplicating that prefix.
CREATE INDEX idx_watch_preferences_target ON watch_preferences (target_kind, target_id);
