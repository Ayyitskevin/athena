-- 0072_activity_chain: a hash chain over the activity trail, so the trail can
-- prove itself. FORWARD-ONLY: once applied anywhere, never edit this file — add
-- 0073_*.sql.
--
-- The activity log is the product's load-bearing promise: "Grok closed AEGIS-88"
-- as a recorded fact. Until now that promise was append-only BY CONVENTION —
-- record() only inserts, and a handful of rows referenced by other tables are
-- DB-frozen (0058) — but nothing let an operator VERIFY that the trail they are
-- reading is the trail that was written. This migration adds the verification
-- substrate: every new activity row gets a sidecar chain row whose entry_hash
-- covers the row's stored facts plus the previous entry's hash, so any edit,
-- insertion, or mid-stream deletion breaks every hash after it.
--
-- ADAPTED, NOT COPIED, from Buzz's "every action is a signed event in one log":
-- Buzz signs relay events with per-member keys; Athena is a single-operator tool
-- whose server holds every key, so a signature would only attest "the box signed
-- what the box wrote". What is worth adopting is VERIFIABILITY — a bounded,
-- deterministic recomputation any reader can run. No keys, no new dependencies,
-- one SHA-256 per write.
--
-- THE CHAIN STARTS AT ADOPTION. Migrations are pure SQL and SQLite cannot
-- compute SHA-256, so rows recorded before this migration are not retroactively
-- chained — and Athena does not pretend otherwise. The first chained row is the
-- anchor; verification reports what lies below it as "before the chain" rather
-- than folding unverifiable history into a verified claim. A fresh database is
-- fully covered from its first row.
--
-- WHAT THIS DOES NOT CLAIM (docs/TRAIL_INTEGRITY.md carries the full list): a
-- writer who can rewrite BOTH tables can rebuild the whole chain, and deleting
-- the newest rows of both tables together leaves a shorter but self-consistent
-- chain. The chain makes tampering EVIDENT against everything short of a full
-- deliberate rebuild, and the head hash exists precisely so an operator can note
-- it somewhere Athena does not control (a backup log, a notebook) and make even
-- that rebuild detectable.

CREATE TABLE activity_chain (
    -- One chain entry per activity row; the trail's own id order IS the chain
    -- order, so the linkage needs no second sequence.
    event_id       INTEGER PRIMARY KEY REFERENCES activity(id),
    -- Versions the HASH RECIPE (which fields, in what canonical form). Bumping
    -- it is a deliberate migration, so the CHECK pins the only recipe this
    -- schema knows how to verify.
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    -- The previous entry's entry_hash, or the documented genesis value (64
    -- zeros) for the first chained row. Stored — not recomputed at read time —
    -- so a verifier can point at the exact link that broke.
    prev_hash      TEXT    NOT NULL
        CHECK (length(prev_hash) = 64 AND prev_hash = lower(prev_hash)),
    -- SHA-256 (hex) over the canonical serialization of the activity row's
    -- stored facts plus prev_hash. The recipe lives in core/activity_chain.py;
    -- it covers only immutable stored columns (never joined display names).
    entry_hash     TEXT    NOT NULL
        CHECK (length(entry_hash) = 64 AND entry_hash = lower(entry_hash))
);

-- The chain is append-only in the strictest sense: an entry is never edited and
-- never removed. There is no "fix the chain" write — a broken chain is a finding
-- to investigate, not a row to repair.
CREATE TRIGGER activity_chain_no_update
BEFORE UPDATE ON activity_chain
BEGIN
    SELECT RAISE(ABORT, 'activity chain entries are immutable');
END;

CREATE TRIGGER activity_chain_no_delete
BEFORE DELETE ON activity_chain
BEGIN
    SELECT RAISE(ABORT, 'activity chain entries are durable');
END;

-- Entries land in id order, each linked to the current head. SQLite cannot
-- check the HASH, but it can check the LINKAGE SHAPE: a new entry must extend
-- the chain (no backfilling behind the head) and must name the head's
-- entry_hash as its prev_hash (the genesis value is only valid when the chain
-- is empty). This closes raw-SQL insertion of a plausible-looking side branch.
CREATE TRIGGER activity_chain_extends_head
BEFORE INSERT ON activity_chain
WHEN EXISTS (
        SELECT 1 FROM activity_chain c WHERE c.event_id >= NEW.event_id
    )
  OR NEW.prev_hash IS NOT COALESCE(
        (SELECT c.entry_hash FROM activity_chain c
          ORDER BY c.event_id DESC LIMIT 1),
        '0000000000000000000000000000000000000000000000000000000000000000'
    )
BEGIN
    SELECT RAISE(ABORT, 'activity chain entries must extend the head');
END;

-- Deliberately ABSENT: triggers freezing the chained activity rows themselves.
-- Deletion of a chained row is already refused twice over — the chain entry's
-- foreign key blocks it, and the entry itself cannot be deleted first — and
-- edit-prevention at the SQL layer would be theater against the actual threat
-- (a writer with the file bypasses triggers entirely) while breaking the
-- legitimate owner of raw UPDATE: nothing in the app edits activity rows, and
-- prevention is not this feature's claim. DETECTION is: an edited row no
-- longer matches its entry_hash, and verification names it. The 0058
-- referenced-row freezes stand unchanged.
