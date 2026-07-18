-- 0049_user_paused: the operator lever between "watch" and "revoke everything".
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0050_*.sql.
--
-- Pausing freezes an account without destroying anything: every authenticated
-- action is refused at identity resolution while the pause holds, and resume
-- restores it exactly - no tokens revoked, no sessions burned, no role change.
-- NULL = active; a timestamp records when the pause began (the who/why lives in
-- the activity trail via the paused_user/resumed_user events).

ALTER TABLE users ADD COLUMN paused_at TEXT;
