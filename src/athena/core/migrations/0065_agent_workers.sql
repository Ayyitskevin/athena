-- 0065_agent_workers: cooperative worker/node registration and an actionable,
-- cooperative kill signal.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0066_*.sql.
--
-- Athena models WHO an agent is (users, tokens, scopes) but has never modeled WHERE
-- it runs. Check-ins (0046) prove a credential reported a RUN recently; they carry
-- no control flag on purpose, so a stale row can never read as "killed". That leaves
-- the operator with no answer to "which of my agent processes are up, on what box,
-- and can I tell one to stop?".
--
-- A worker is a row that heartbeats. There is no cluster, no scheduler, and no
-- leader election here: Athena stores what a worker reports about itself and one
-- instruction it may be given back.
--
-- THE KILL IS COOPERATIVE, AND THE SCHEMA SAYS SO. Athena cannot signal a foreign
-- OS process. It can only record that an operator ASKED a worker to stop
-- (kill_requested_at), that the worker SAID it received the request
-- (kill_acknowledged_at), and that the worker SAID it stopped (stopped_at). Three
-- separate columns because they are three separate facts, and collapsing them into
-- one "killed" flag would be exactly the lie this schema exists to prevent: a
-- worker that goes silent after a kill request is STALE, never "terminated".

CREATE TABLE agent_workers (
    -- Surrogate id so an operator addresses one worker unambiguously
    -- (POST /workers/{id}/kill) without a compound path or cross-agent collisions.
    id                   INTEGER PRIMARY KEY,
    agent_id             INTEGER NOT NULL REFERENCES users(id),
    -- The worker's own name for itself, scoped to its agent: opaque client text,
    -- like a run id. Two agents may both call a worker "worker-1" without either
    -- reaching the other's row.
    worker_key           TEXT    NOT NULL
        CHECK (length(worker_key) BETWEEN 1 AND 200 AND worker_key = trim(worker_key)),
    -- Where it says it runs, and what it says it can do. Self-declared and echoed
    -- back verbatim: Athena never routes, schedules, or authorizes on either.
    node_label           TEXT    NOT NULL DEFAULT ''
        CHECK (length(node_label) <= 200),
    capabilities         TEXT    NOT NULL DEFAULT ''
        CHECK (length(capabilities) <= 2000),
    first_seen_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Which credential last reported, for forensics. Never exposed on a read
    -- projection, mirroring agent_run_checkins.last_token_id.
    last_token_id        INTEGER NOT NULL REFERENCES api_tokens(id),
    kill_requested_at    TEXT,
    kill_requested_by    INTEGER REFERENCES users(id),
    kill_acknowledged_at TEXT,
    stopped_at           TEXT,
    UNIQUE (agent_id, worker_key),
    CHECK (last_seen_at >= first_seen_at),
    -- A worker cannot acknowledge an instruction nobody gave, and a request always
    -- names the operator who gave it.
    CHECK (kill_acknowledged_at IS NULL OR kill_requested_at IS NOT NULL),
    CHECK ((kill_requested_at IS NULL) = (kill_requested_by IS NULL))
);

-- The cockpit reads one agent's most recently reporting workers first.
CREATE INDEX idx_agent_workers_recent
    ON agent_workers (agent_id, last_seen_at DESC);

-- The operator's actual question — "who was told to stop and has not answered?" —
-- served without scanning the registry. Partial, so the quiet majority costs
-- nothing.
CREATE INDEX idx_agent_workers_unacknowledged_kill
    ON agent_workers (kill_requested_at)
    WHERE kill_requested_at IS NOT NULL AND kill_acknowledged_at IS NULL;
