-- 0062_agent_budgets: a durable, opt-in cap on how many metered writes one agent
-- may make per fixed window.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0063_*.sql.
--
-- Athena's per-token rate limiter lives in process memory: it bounds a burst but
-- forgets everything on restart, so it is not a budget. This table is the durable
-- half of "every agent action is attributable, reversible, and BOUNDED" -- the
-- operator sets a ceiling, and the metered commands decrement it inside their own
-- write transaction, so a mutation and its charge commit or roll back together.
--
-- OPT-IN BY DESIGN: no row means unlimited. An agent is unbudgeted until a human
-- admin sets one, exactly like the blocked-close policy in 0060, so applying this
-- migration changes nothing about existing behavior.
--
-- WHAT IS METERED: durable domain writes, counted once per accepted command --
-- not tokens, dollars, or model spend. Athena is the control plane; it never sees
-- what an agent's process spends on inference, so it deliberately does NOT carry a
-- cost column it could not honestly populate. A cost meter can be added additively
-- if an external reporter ever supplies one.
--
-- WINDOW SEMANTICS: a FIXED window, not a sliding one. window_started_at marks the
-- start of the current period; the first charge at or after start+length resets the
-- counter and re-anchors the window. Fixed windows permit a burst across a boundary
-- (up to 2x the limit in one window-length); that is an accepted, documented
-- trade-off for a counter an operator can reason about without a background job.

CREATE TABLE agent_budgets (
    user_id           INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    -- The window length. Kept as a small closed set so the projection can label it
    -- without parsing durations.
    window            TEXT NOT NULL CHECK (window IN ('hour', 'day')),
    -- Maximum metered actions per window. 0 is a legitimate value: a hard freeze on
    -- durable writes that still leaves reads working (unlike pause, which refuses
    -- every authenticated action).
    action_limit      INTEGER NOT NULL CHECK (action_limit >= 0),
    action_used       INTEGER NOT NULL DEFAULT 0 CHECK (action_used >= 0),
    window_started_at TEXT NOT NULL,
    set_by            INTEGER REFERENCES users(id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
