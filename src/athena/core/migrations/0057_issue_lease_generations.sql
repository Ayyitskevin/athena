-- 0057_issue_lease_generations: fence every lease possession with an opaque epoch.
-- A holder id is not enough: the same actor may yield or expire generation A, acquire
-- generation B, and then deliver a delayed A command. The generation lets commands
-- reject that stale mutation without retaining deleted lease tombstones.
-- FORWARD-ONLY: older applications cannot create a generation-bearing lease after this
-- migration; restore a pre-0057 backup before rolling application code back.

ALTER TABLE issue_leases ADD COLUMN generation TEXT;

-- Existing leases become one honest legacy possession each. SQLite owns this one-time
-- backfill; runtime acquisitions use Python's cryptographic token generator.
UPDATE issue_leases SET generation = lower(hex(randomblob(16)));

CREATE UNIQUE INDEX idx_issue_leases_generation
    ON issue_leases (generation);

CREATE TRIGGER issue_leases_generation_required_insert
BEFORE INSERT ON issue_leases
WHEN NEW.generation IS NULL
  OR length(NEW.generation) <> 32
  OR NEW.generation GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'valid issue lease generation required');
END;

CREATE TRIGGER issue_leases_generation_required_update
BEFORE UPDATE OF generation ON issue_leases
WHEN NEW.generation IS NULL
  OR length(NEW.generation) <> 32
  OR NEW.generation GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'valid issue lease generation required');
END;

-- A live possession's generation is immutable, including for raw SQL writers.
-- Expired rows may rotate it during the compare-and-swap acquisition upsert.
CREATE TRIGGER issue_leases_active_generation_immutable
BEFORE UPDATE OF generation ON issue_leases
WHEN OLD.expires_at > datetime('now')
  AND NEW.generation IS NOT OLD.generation
BEGIN
    SELECT RAISE(ABORT, 'active issue lease generation is immutable');
END;

-- A live possession is never transferable. An expired row may be reacquired by
-- another eligible holder, but only together with the fresh generation required below.
CREATE TRIGGER issue_leases_active_holder_immutable
BEFORE UPDATE OF holder_id ON issue_leases
WHEN OLD.expires_at > datetime('now')
  AND NEW.holder_id IS NOT OLD.holder_id
BEGIN
    SELECT RAISE(ABORT, 'active issue lease holder is immutable');
END;

-- Reacquisition is one atomic epoch change. Merely extending an expired row cannot
-- resurrect an old capability, even for the same holder or a raw SQL writer.
CREATE TRIGGER issue_leases_reactivation_rotates_generation
BEFORE UPDATE OF expires_at, generation ON issue_leases
WHEN OLD.expires_at <= datetime('now')
  AND NEW.expires_at > datetime('now')
  AND NEW.generation IS OLD.generation
BEGIN
    SELECT RAISE(ABORT, 'reactivated issue lease requires a new generation');
END;

CREATE TRIGGER issue_leases_holder_change_rotates_generation
BEFORE UPDATE OF holder_id, generation ON issue_leases
WHEN NEW.holder_id IS NOT OLD.holder_id
  AND NEW.generation IS OLD.generation
BEGIN
    SELECT RAISE(ABORT, 'issue lease holder change requires a new generation');
END;

CREATE TRIGGER issue_leases_issue_immutable
BEFORE UPDATE OF issue_id ON issue_leases
WHEN NEW.issue_id IS NOT OLD.issue_id
BEGIN
    SELECT RAISE(ABORT, 'issue lease identity is immutable');
END;
