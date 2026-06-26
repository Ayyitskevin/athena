-- 0023_notifications: watching + a per-user inbox over the event log.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0024.
--
-- A notification is the PUSH-to-a-human twin of the event feed: when something
-- happens to a target you watch, you get an inbox entry pointing at the activity
-- event that caused it. The audit log stays the single source of truth — a
-- notification carries no copy of the event, just a reference, so the inbox renders
-- from the same row the feed does.

-- Who watches what. Composite PK makes a (user, target) pair unique and idempotent
-- (watching twice is a no-op). A target is polymorphic ('issue' | 'page'), like
-- activity/links, so no foreign key on target_id.
CREATE TABLE watches (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    target_kind TEXT    NOT NULL,
    target_id   INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, target_kind, target_id)
);

-- The fan-out query is "who watches this target" — index the target end.
CREATE INDEX idx_watches_target ON watches (target_kind, target_id);

-- One inbox row per (user, event). event_id references a real activity row; the
-- UNIQUE pair stops a user being notified twice for the same event. read_at NULL
-- means unread. ON the activity FK there is no ON DELETE — the trail is append-only
-- and never deleted, so a notification can't dangle.
CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    event_id   INTEGER NOT NULL REFERENCES activity(id),
    read_at    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, event_id)
);

-- Two hot reads: a user's inbox (newest first) and their unread count; both key on
-- user_id, and the unread count filters read_at.
CREATE INDEX idx_notifications_user ON notifications (user_id, read_at, id);
