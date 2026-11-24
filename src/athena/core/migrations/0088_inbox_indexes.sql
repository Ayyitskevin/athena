-- 0088_inbox_indexes: inbox reads use an index over what they return
-- (spec H4, finding PERF-7).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add a new one.
--
-- Notifications are never deleted (retention is H20), so a read that walks a
-- user's rows grows with history. 0023's (user_id, read_at, id) index served
-- the unread reads, but the newest-first list of every row could not use it in
-- id order: SQLite read all of the user's rows through the (user_id, event_id)
-- unique index and sorted them in a temporary B-tree before its LIMIT.
--
--   idx_notifications_unread  the unread rows only, by user, newest first, so an
--                             unread count reads unread rows alone and an
--                             unread list stops at its limit. Reading a row
--                             removes it from here. The event id is NOT in it:
--                             with it, SQLite planned an acknowledgement by id
--                             as a probe of every visible event for every id
--                             (22 s where it took 7 s at 50,000 issues, inside
--                             the writer lock).
--                             read_at (always NULL in it) is a column so that
--                             `read_at IS NULL` is an equality term on it: with
--                             no statistics (Athena never runs ANALYZE) SQLite
--                             otherwise picks the inbox index below and walks
--                             read rows too.
--   idx_notifications_inbox   every row, by user, in id order, so the
--                             newest-first list stops at its limit.
--
-- Between them they serve every read 0023's index did, so it is dropped rather
-- than kept as a third index every notification insert would have to update.
--
-- Building them on an existing database reads the notifications table once per
-- index and holds the writer lock while it does (the runner wraps this file and
-- its ledger row in one transaction). Interrupted, it rolls back whole; run
-- again (by hand, after it has been applied), it changes nothing.

CREATE INDEX IF NOT EXISTS idx_notifications_unread
    ON notifications (user_id, read_at, id) WHERE read_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_notifications_inbox
    ON notifications (user_id, id);

DROP INDEX IF EXISTS idx_notifications_user;
