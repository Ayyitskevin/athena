-- Active-work supervision reads current credential posture for a bounded set of
-- holders. Revoked token history is intentionally retained, so index only live
-- rows and let the planner seek by holder instead of rescanning all token history.

CREATE INDEX idx_api_tokens_live_user
    ON api_tokens (user_id)
    WHERE revoked_at IS NULL;
