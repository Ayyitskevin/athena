-- 0061_idempotency_pause_fence: account pause is an authority change.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0062_*.sql.
--
-- The durable idempotency fence (0043) bumps a global authorization revision
-- whenever current authorization may be NARROWER than it was at claim time,
-- permanently fencing stored response bodies. Account pause (0049) is exactly
-- such a narrowing -- "every authenticated action is refused at identity
-- resolution" -- but paused_at was not among the fenced columns, so a stored
-- response claimed before a pause remained replayable under it (and after a
-- resume) by any still-live credential. Fence on any pause-state change; resume
-- bumps too, which is intentionally broad, exactly like the role trigger --
-- expansions cost a re-execution, never a wrong disclosure.
CREATE TRIGGER idempotency_authz_user_paused
AFTER UPDATE OF paused_at ON users
WHEN NEW.paused_at IS NOT OLD.paused_at
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;
