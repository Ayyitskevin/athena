-- 0067_icarus_dispatch: the record of work Athena handed to an external executor.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0068_*.sql.
--
-- Athena is the control plane; an execution fleet (Icarus) is a separate system
-- with its own store. They share NO database and neither imports the other: they
-- reconcile over an asynchronous HTTP contract, and this table is Athena's half of
-- it.
--
-- WHAT THIS TABLE KNOWS IS WHAT ATHENA WAS TOLD. Every column below is either a
-- fact Athena produced (the work item, the run it minted, the policy digest it
-- computed, the moment it asked) or a claim the executor returned (its own run id,
-- an evidence pointer, a completion pointer). The `state` column names Athena's
-- knowledge, never the executor's progress: `accepted` means "it said it accepted",
-- not "work is running". Athena cannot see the far side and does not pretend to.
--
-- Evidence is REFERENCED, never copied: evidence_ref and completion_ref are opaque
-- strings the executor chose. Copying artifacts across the boundary would make one
-- system's storage the other's problem, which is exactly what "no shared database"
-- rules out.

CREATE TABLE icarus_dispatches (
    id              INTEGER PRIMARY KEY,
    -- The Aegis work item being executed. Deliberately no ON DELETE CASCADE: a
    -- dispatch is a record that Athena asked something of the outside world, and
    -- that stays true after the issue is gone.
    work_item_id    INTEGER NOT NULL REFERENCES issues(id),
    -- The reserved `icarus:` run Athena mints for this dispatch, and the run that
    -- spawned it. Fork/child runs are DERIVED from the activity trail's
    -- parent_run_id rather than stored here — one place for lineage, not two.
    run_id          TEXT    NOT NULL UNIQUE,
    parent_run_id   TEXT,
    -- The executor's own run id, opaque to Athena, set when it accepts. NULL until
    -- then, and NULL forever if it never answers.
    icarus_run_id   TEXT,
    repo            TEXT    NOT NULL CHECK (length(repo) BETWEEN 1 AND 400),
    base_commit     TEXT    NOT NULL CHECK (length(base_commit) BETWEEN 1 AND 200),
    capability      TEXT    NOT NULL CHECK (length(capability) BETWEEN 1 AND 100),
    -- A tamper-evident hash of the authorization state in force when Athena asked.
    -- A callback carrying a different digest is recorded and FLAGGED rather than
    -- trusted: the point of a digest is to notice, not to prevent.
    policy_digest   TEXT    NOT NULL,
    approval_state  TEXT    NOT NULL DEFAULT 'not_required'
        CHECK (approval_state IN ('not_required', 'approved')),
    -- Single-flight: re-dispatching the same intent reuses this key rather than
    -- asking the executor twice.
    idempotency_key TEXT    NOT NULL UNIQUE,
    evidence_ref    TEXT,
    completion_ref  TEXT,
    -- Athena's knowledge, not the executor's progress. pending_delivery: recorded,
    -- not yet acknowledged. accepted: it said it accepted. undeliverable: Athena
    -- could not hand it over. completed/failed: it reported a terminal outcome.
    state           TEXT    NOT NULL DEFAULT 'pending_delivery'
        CHECK (state IN (
            'pending_delivery', 'accepted', 'undeliverable', 'completed', 'failed'
        )),
    -- Why a delivery failed, for the operator. NULL when nothing failed.
    last_error      TEXT,
    dispatched_by   INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The operator's view: this issue's dispatches, newest first.
CREATE INDEX idx_icarus_dispatches_work_item
    ON icarus_dispatches (work_item_id, id DESC);

-- The callback's lookup: map an executor's run id back to the dispatch. Partial,
-- because a dispatch has no icarus_run_id until it is accepted.
CREATE UNIQUE INDEX idx_icarus_dispatches_icarus_run
    ON icarus_dispatches (icarus_run_id) WHERE icarus_run_id IS NOT NULL;

-- "What is still out there?" — served without scanning finished work.
CREATE INDEX idx_icarus_dispatches_open
    ON icarus_dispatches (state, id DESC)
    WHERE state IN ('pending_delivery', 'accepted');
