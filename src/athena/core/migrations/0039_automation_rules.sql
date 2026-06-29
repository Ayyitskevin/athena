-- 0039_automation_rules: "when event X, do Y" automation over the activity log.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0040.
--
-- Athena already PUSHES each new activity event to webhooks and lets consumers PULL
-- the feed. This adds a third, INTERNAL consumer: a rules engine that reacts to the
-- same append-only `activity` log and performs an in-app action (assign an issue, add
-- a label, change status, comment). It is the lean, event-driven automation a mover
-- expects ("when an issue lands in Triage, assign it to Bob") without importing a
-- workflow engine — the blackboard pattern over the log Athena already owns.
--
-- A rule is: a TRIGGER (an activity verb + target kind, e.g. 'changed_status' on an
-- 'issue', or '*' for any verb) narrowed by optional CONDITIONS (a JSON object matched
-- against the target issue's current fields, e.g. {"project_id": 3, "status": "done"}),
-- and an ACTION (a type + JSON params, e.g. type 'assign', params {"user_id": 5}).
-- Conditions/params are stored as JSON text and validated at the boundary — the engine
-- and data layer stay schema-light, like saved_filters' criteria blob.

CREATE TABLE automation_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    -- Disabled rules stay configured but never fire (the pause twin of a webhook's
    -- active flag), so an operator can switch one off without losing it.
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- The event to react to. trigger_verb matches activity.verb; the literal '*'
    -- matches any verb. target_kind matches activity.target_kind (issues for now).
    trigger_verb  TEXT    NOT NULL,
    target_kind   TEXT    NOT NULL DEFAULT 'issue',
    -- Optional match conditions as a JSON object; '{}' (the default) matches every
    -- event of the trigger. Keys are issue fields resolved at match time.
    conditions    TEXT    NOT NULL DEFAULT '{}',
    -- The action: a type plus JSON params. The boundary validates both against the
    -- supported action set; the engine dispatches on action_type.
    action_type   TEXT    NOT NULL,
    action_params TEXT    NOT NULL DEFAULT '{}',
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The rules engine is ONE consumer of the log, so it keeps ONE cursor (unlike each
-- webhook's per-subscriber cursor): the id of the last activity row it processed.
-- Single-row table (id pinned to 1). It starts at the CURRENT activity tip at migration
-- time, so the engine only ever reacts to events recorded AFTER automation was added —
-- rules never fire retroactively over pre-existing history. COALESCE handles a fresh,
-- empty install (no activity yet → 0).
CREATE TABLE automation_state (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    cursor INTEGER NOT NULL DEFAULT 0
);
INSERT INTO automation_state (id, cursor)
    VALUES (1, COALESCE((SELECT MAX(id) FROM activity), 0));
