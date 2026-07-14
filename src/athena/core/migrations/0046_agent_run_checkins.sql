-- 0046_agent_run_checkins: cooperative, server-timed run presence.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0047_*.sql.
--
-- Activity remains the immutable record of what an agent DID. A check-in is a small,
-- mutable operational fact: this agent's bearer credential reported this run recently.
-- It deliberately carries no client timestamp, progress payload, terminal state, or
-- control flag, so a stale row can never be mistaken for a finished/killed process.

CREATE TABLE agent_run_checkins (
    agent_id      INTEGER NOT NULL REFERENCES users(id),
    run_id        TEXT    NOT NULL
        CHECK (length(run_id) BETWEEN 1 AND 200 AND run_id = trim(run_id)),
    first_seen_at TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_token_id INTEGER NOT NULL REFERENCES api_tokens(id),
    PRIMARY KEY (agent_id, run_id),
    CHECK (last_seen_at >= first_seen_at)
);

-- Mission control reads one agent's newest cooperative check-ins first. The PK
-- remains the conflict target that makes concurrent first heartbeats converge.
CREATE INDEX idx_agent_run_checkins_recent
    ON agent_run_checkins (agent_id, last_seen_at DESC, run_id);
