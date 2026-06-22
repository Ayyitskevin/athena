-- 0006_priority: how urgent an issue is.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0007_*.sql.

-- A required priority with a sane default. NOT NULL is allowed on ADD COLUMN
-- because we give a literal DEFAULT, so every existing row backfills to 'medium'
-- (the neutral middle of low/medium/high/urgent). Mirrors how `status` defaults
-- to 'open' in the initial schema. Lifecycle validation lives at the boundary.
ALTER TABLE issues ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium';
