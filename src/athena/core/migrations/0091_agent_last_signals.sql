-- 0091_agent_last_signals: what an agent was last refused and when it last
-- found nothing ready, and an index for completions that reported HOLD or
-- BLOCKED (spec F15).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add a new one.
--
-- A seat that is refused, or that asks for work and is told there is none,
-- wrote nothing an operator could read: a refusal is an answer, not an event,
-- and the trail holds only what happened. An operator looking at an idle agent
-- could not tell "refused on every call" from "nothing to do" from "not
-- running".
--
-- One row per agent account, replaced in place: the newest refusal (its
-- registered code, HTTP status and the request it answered) and the newest
-- time a pickup answered `nothing_ready`. It is not a log. A row per refusal
-- would let a looping agent grow the table without bound, and the question the
-- operator asks is "what is it hitting now", which the newest answers. The
-- refusals that are themselves events (a scope denial, a paused account, a
-- spent budget) stay on the trail as before.
--
-- Not part of the activity trail and not in its hash chain: these are answers
-- the server gave, kept for diagnosis, and replaced without history.
CREATE TABLE agent_last_signals (
    user_id               INTEGER PRIMARY KEY REFERENCES users(id),
    last_refusal_code     TEXT
        CHECK (last_refusal_code IS NULL
               OR length(last_refusal_code) BETWEEN 1 AND 100),
    last_refusal_status   INTEGER
        CHECK (last_refusal_status IS NULL
               OR last_refusal_status BETWEEN 400 AND 499),
    last_refusal_request  TEXT
        CHECK (last_refusal_request IS NULL
               OR length(last_refusal_request) BETWEEN 1 AND 300),
    last_refusal_at       TEXT,
    last_nothing_ready_at TEXT,
    -- A refusal is its four facts together, or absent.
    CHECK ((last_refusal_code IS NULL) = (last_refusal_at IS NULL)
       AND (last_refusal_code IS NULL) = (last_refusal_status IS NULL)
       AND (last_refusal_code IS NULL) = (last_refusal_request IS NULL))
);

-- The attention read asks for the completions whose reported verdict is HOLD
-- or BLOCKED. 0086 stores the verdict of every completion, most of them PASS;
-- this partial index holds only the ones that read asks for.
CREATE INDEX idx_activity_event_facts_held
    ON activity_event_facts (event_id)
    WHERE verdict IN ('HOLD', 'BLOCKED');
