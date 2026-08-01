-- 0070_run_controls: identity-bound operator control requests for live agent runs.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0071_*.sql.
--
-- VISION.md's Intervene step lets the operator pause an agent or ask a worker
-- process to stop, but between "let it run" and "kill it" there was nothing: no
-- way to hand a LIVE RUN bounded guidance ("steer"), ask it to wind down this one
-- run cooperatively ("request_cancel"), or ask it to close out with a structured
-- handoff so a fresh context can continue ("request_fresh_context"). A control is
-- that middle lever: an operator-issued, run-addressed request that only the
-- run's own agent can consume and answer.
--
-- A RUN STAYS A PROJECTION. There is still no runs table: a control names a run
-- id and pins the agent that owned it at admission (resolved from run_bindings,
-- or the run's sole cooperative check-in). The control row is a request record —
-- like approval_requests (0063) and the worker kill columns (0065) — never a
-- second source of truth about what the run did; that remains the activity log.
--
-- THE LIFECYCLE IS COOPERATIVE, AND THE SCHEMA SAYS SO. Following 0065's
-- doctrine, each fact an actor claimed is its own timestamp column:
-- an operator ASKED (created_at/requested_by), the agent SAID it received
-- (acknowledged_at), the agent SAID how it ended (settled_at/settled_by/
-- settlement + bounded result). Expiry is deliberately NOT a column: it is
-- derived at read time from expires_at against the server clock, because "the
-- agent never answered" is an observation, not an event anyone performed — the
-- same reason a silent worker is stale, never "terminated".
--
-- Acknowledgement proves receipt. Completion is an identity-bound claim.
-- Neither proves an operating-system effect, and no projection over this table
-- may say otherwise.

CREATE TABLE run_controls (
    -- Surrogate id so operator and agent address one control unambiguously.
    id                    INTEGER PRIMARY KEY,
    -- Versions the CONTROL RECORD shape. Bumping it is a deliberate migration,
    -- so the CHECK pins the only version this schema knows how to hold.
    schema_version        INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    -- The target run: an opaque client-chosen id, same shape as run_bindings.
    run_id                TEXT    NOT NULL
        CHECK (length(run_id) BETWEEN 1 AND 200 AND run_id = trim(run_id)),
    -- The agent that owned the run when the control was admitted. Settlement is
    -- restricted to this identity; if the run's ownership story changes later,
    -- settlement refuses rather than guessing.
    agent_id              INTEGER NOT NULL REFERENCES users(id),
    -- Optional targeting metadata: which registered worker process the operator
    -- meant. Workers authenticate with their agent's token, so this cannot be
    -- credential-enforced — it narrows intent, never authority.
    worker_id             INTEGER REFERENCES agent_workers(id),
    -- The closed v1 control vocabulary. New kinds are a migration, on purpose:
    -- each kind is a promise about what settlement means.
    kind                  TEXT    NOT NULL
        CHECK (kind IN ('steer', 'request_cancel', 'request_fresh_context')),
    -- Bounded operator guidance or reason. Never executed, never interpolated —
    -- delivered verbatim to the bound agent and nobody else.
    payload               TEXT    NOT NULL DEFAULT ''
        CHECK (length(payload) <= 4000),
    requested_by          INTEGER NOT NULL REFERENCES users(id),
    -- Domain-level single-flight: one control per (requester, key), enforced by
    -- the unique index below. Minted server-side when the caller omits it.
    idempotency_key       TEXT    NOT NULL
        CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    -- After this instant an unsettled control reads as expired and refuses
    -- settlement. Compared against an injectable server clock in the command
    -- layer, never trusted from any client.
    expires_at            TEXT    NOT NULL,
    -- The agent's claims, each set at most once.
    acknowledged_at       TEXT,
    settled_at            TEXT,
    settled_by            INTEGER REFERENCES users(id),
    settlement            TEXT
        CHECK (settlement IN ('completed', 'declined')),
    -- Bounded settlement answer: the decline reason, or the completion summary.
    result_summary        TEXT    NOT NULL DEFAULT ''
        CHECK (length(result_summary) <= 2000),
    -- The structured fresh-context handoff (summary, unresolved questions,
    -- Athena ids, evidence refs) — a bounded JSON object, only ever for
    -- request_fresh_context. Never transcripts, never hidden reasoning.
    result_payload        TEXT
        CHECK (
            result_payload IS NULL
            OR (
                json_valid(result_payload)
                AND json_type(result_payload) = 'object'
                AND length(result_payload) <= 8000
            )
        ),
    -- Trail correlation: the activity events that recorded each fact, so replay
    -- and the control row can vouch for each other.
    requested_event_id    INTEGER REFERENCES activity(id),
    acknowledged_event_id INTEGER REFERENCES activity(id),
    settled_event_id      INTEGER REFERENCES activity(id),
    CHECK (expires_at > created_at),
    -- Settlement facts land together or not at all.
    CHECK ((settled_at IS NULL) = (settlement IS NULL)),
    CHECK ((settled_at IS NULL) = (settled_by IS NULL)),
    -- A settlement always says something; an unsettled control says nothing.
    CHECK (settled_at IS NOT NULL OR (result_summary = '' AND result_payload IS NULL)),
    CHECK (settlement IS NULL OR result_summary <> ''),
    -- The structured handoff belongs to fresh-context completions only.
    CHECK (result_payload IS NULL OR kind = 'request_fresh_context'),
    CHECK (result_payload IS NULL OR settlement = 'completed'),
    -- An answer's event cannot exist before the answer.
    CHECK (acknowledged_event_id IS NULL OR acknowledged_at IS NOT NULL),
    CHECK (settled_event_id IS NULL OR settled_at IS NOT NULL)
);

-- The run detail panel reads one run's controls newest-first.
CREATE INDEX idx_run_controls_run ON run_controls (run_id, id DESC);

-- The bound agent's actual question — "what is addressed to me and unanswered?"
-- — served without scanning settled history. Expired-but-unsettled rows remain
-- here on purpose: expiry is derived at read time, not stored.
CREATE INDEX idx_run_controls_agent_open
    ON run_controls (agent_id, id DESC)
    WHERE settled_at IS NULL;

-- Domain idempotency: replaying the same key returns the same control; reusing
-- it for a different control is refused. The command compares the bound fields;
-- this index closes the raced double-insert.
CREATE UNIQUE INDEX idx_run_controls_idempotency
    ON run_controls (requested_by, idempotency_key);

-- A control is born unanswered. Every later fact arrives through its own
-- guarded transition below.
CREATE TRIGGER run_controls_must_start_unanswered
BEFORE INSERT ON run_controls
WHEN NEW.acknowledged_at IS NOT NULL
  OR NEW.settled_at IS NOT NULL
  OR NEW.settled_by IS NOT NULL
  OR NEW.settlement IS NOT NULL
  OR NEW.result_summary <> ''
  OR NEW.result_payload IS NOT NULL
  OR NEW.requested_event_id IS NOT NULL
  OR NEW.acknowledged_event_id IS NOT NULL
  OR NEW.settled_event_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'run control must start unanswered');
END;

-- What the operator asked is immutable. Steering by silently editing a live
-- request would put words in the operator's mouth after the agent read them.
CREATE TRIGGER run_controls_request_immutable
BEFORE UPDATE ON run_controls
WHEN NEW.id IS NOT OLD.id
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.agent_id IS NOT OLD.agent_id
  OR NEW.worker_id IS NOT OLD.worker_id
  OR NEW.kind IS NOT OLD.kind
  OR NEW.payload IS NOT OLD.payload
  OR NEW.requested_by IS NOT OLD.requested_by
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
    SELECT RAISE(ABORT, 'run control request is immutable');
END;

-- Each claim is stated once and never retracted or rewritten.
CREATE TRIGGER run_controls_claims_write_once
BEFORE UPDATE ON run_controls
WHEN (OLD.acknowledged_at IS NOT NULL
      AND NEW.acknowledged_at IS NOT OLD.acknowledged_at)
  OR (OLD.requested_event_id IS NOT NULL
      AND NEW.requested_event_id IS NOT OLD.requested_event_id)
  OR (OLD.acknowledged_event_id IS NOT NULL
      AND NEW.acknowledged_event_id IS NOT OLD.acknowledged_event_id)
  OR (OLD.settled_event_id IS NOT NULL
      AND NEW.settled_event_id IS NOT OLD.settled_event_id)
BEGIN
    SELECT RAISE(ABORT, 'run control claims are write-once');
END;

-- A settled control is frozen except for binding its settlement event, which
-- lands in the same command transaction immediately after the audit row exists.
CREATE TRIGGER run_controls_settled_frozen
BEFORE UPDATE ON run_controls
WHEN OLD.settled_at IS NOT NULL
 AND (NEW.settled_at IS NOT OLD.settled_at
      OR NEW.settled_by IS NOT OLD.settled_by
      OR NEW.settlement IS NOT OLD.settlement
      OR NEW.result_summary IS NOT OLD.result_summary
      OR NEW.result_payload IS NOT OLD.result_payload
      OR NEW.acknowledged_at IS NOT OLD.acknowledged_at)
BEGIN
    SELECT RAISE(ABORT, 'settled run control is immutable');
END;

-- Trail correlation may only name native, unrestricted events that actually
-- record this control's facts, spoken by the right actor. Imported history can
-- never vouch for a control (0058 precedent).
CREATE TRIGGER run_controls_requested_event_native
BEFORE UPDATE OF requested_event_id ON run_controls
WHEN NEW.requested_event_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM activity AS a
     WHERE a.id = NEW.requested_event_id
       AND a.verb = 'run_control_requested'
       AND a.target_kind = 'run_control'
       AND a.target_id = NEW.id
       AND a.actor_id = NEW.requested_by
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
)
BEGIN
    SELECT RAISE(ABORT, 'matching native run control request event required');
END;

CREATE TRIGGER run_controls_acknowledged_event_native
BEFORE UPDATE OF acknowledged_event_id ON run_controls
WHEN NEW.acknowledged_event_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM activity AS a
     WHERE a.id = NEW.acknowledged_event_id
       AND a.verb = 'run_control_acknowledged'
       AND a.target_kind = 'run_control'
       AND a.target_id = NEW.id
       AND a.actor_id = NEW.agent_id
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
)
BEGIN
    SELECT RAISE(ABORT, 'matching native run control acknowledgement event required');
END;

CREATE TRIGGER run_controls_settled_event_native
BEFORE UPDATE OF settled_event_id ON run_controls
WHEN NEW.settled_event_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM activity AS a
     WHERE a.id = NEW.settled_event_id
       AND a.verb = 'run_control_' || NEW.settlement
       AND a.target_kind = 'run_control'
       AND a.target_id = NEW.id
       AND a.actor_id = NEW.settled_by
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
)
BEGIN
    SELECT RAISE(ABORT, 'matching native run control settlement event required');
END;

-- Controls are durable history: what an operator asked and how the agent
-- answered must survive both of them.
CREATE TRIGGER run_controls_no_delete
BEFORE DELETE ON run_controls
BEGIN
    SELECT RAISE(ABORT, 'run controls are durable');
END;
