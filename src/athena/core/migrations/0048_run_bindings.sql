-- 0048_run_bindings: a run id belongs to the FIRST actor that wrote under it.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0049_*.sql.
--
-- Run ids arrive as client-chosen headers, which until now ANY actor could
-- reuse: agent B could write events into agent A's run and poison its replay,
-- lineage, and Mission Control story. This table pins each run id to the first
-- actor that used it; activity.record() binds on first tagged write and refuses
-- a tagged write by anyone else. Imported events cannot collide (portability
-- forces their run ids to NULL), so record() is the only producer.

CREATE TABLE run_bindings (
    run_id   TEXT PRIMARY KEY
        CHECK (length(run_id) BETWEEN 1 AND 200 AND run_id = trim(run_id)),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    bound_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Backfill: bind every historical run id to the actor of its EARLIEST event.
-- A run id that was (illegitimately) shared by several actors stays bound to
-- the first; later actors lose the ability to continue it — which is the point.
-- Ids that would violate the CHECK (malformed pre-normalization strays) are
-- left unbound and will be claimed by their next live writer instead.
INSERT INTO run_bindings (run_id, actor_id, bound_at)
SELECT run_id, actor_id, created_at
FROM (
    SELECT run_id, actor_id, created_at, MIN(id) AS first_event_id
    FROM activity
    WHERE run_id IS NOT NULL
      AND length(run_id) BETWEEN 1 AND 200
      AND run_id = trim(run_id)
    GROUP BY run_id
);
