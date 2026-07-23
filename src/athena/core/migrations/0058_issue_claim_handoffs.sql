-- 0058_issue_claim_handoffs: typed, generation-bound continuation context for a
-- leaseholder who must release unfinished work. Activity remains the immutable
-- audit/notification stream; this table is the authoritative operational state.
-- Imported activity never creates rows here. Selective portability v1 therefore
-- cannot turn foreign prose into actionable agent instructions.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0059_*.sql.

CREATE TABLE issue_claim_handoffs (
    id                      INTEGER PRIMARY KEY,
    handoff_token           TEXT NOT NULL UNIQUE CHECK (
                                length(handoff_token) = 32 AND
                                handoff_token NOT GLOB '*[^0-9a-f]*'
                            ),
    issue_id                INTEGER NOT NULL REFERENCES issues(id),
    yield_event_id          INTEGER NOT NULL UNIQUE REFERENCES activity(id),
    lease_generation        TEXT NOT NULL CHECK (
                                length(lease_generation) = 32 AND
                                lease_generation NOT GLOB '*[^0-9a-f]*'
                            ),
    schema_version          INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    reason                  TEXT NOT NULL CHECK (
                                reason IN ('needs_input', 'blocked', 'capacity')
                            ),
    note                    TEXT CHECK (note IS NULL OR length(note) <= 500),
    attempted_work          TEXT NOT NULL CHECK (
                                length(trim(
                                    attempted_work,
                                    ' ' || char(9) || char(10) || char(13)
                                )) > 0 AND
                                length(attempted_work) <= 4000
                            ),
    evidence_json           TEXT NOT NULL CHECK (
                                length(evidence_json) <= 8000 AND
                                json_valid(evidence_json) AND
                                json_type(evidence_json) = 'array'
                            ),
    blocking_question       TEXT NOT NULL CHECK (
                                length(trim(
                                    blocking_question,
                                    ' ' || char(9) || char(10) || char(13)
                                )) > 0 AND
                                length(blocking_question) <= 1000
                            ),
    resume_instructions     TEXT NOT NULL CHECK (
                                length(trim(
                                    resume_instructions,
                                    ' ' || char(9) || char(10) || char(13)
                                )) > 0 AND
                                length(resume_instructions) <= 4000
                            ),
    resume_event_id         INTEGER UNIQUE REFERENCES activity(id),
    resume_lease_generation TEXT CHECK (
                                resume_lease_generation IS NULL OR (
                                    length(resume_lease_generation) = 32 AND
                                    resume_lease_generation NOT GLOB '*[^0-9a-f]*'
                                )
                            ),
    resume_note             TEXT CHECK (
                                resume_note IS NULL OR length(resume_note) <= 2000
                            ),
    UNIQUE (issue_id, lease_generation),
    CHECK (
        (resume_event_id IS NULL AND
         resume_lease_generation IS NULL AND
         resume_note IS NULL)
        OR
        (resume_event_id IS NOT NULL AND
         resume_lease_generation IS NOT NULL)
    )
);

-- One unresolved native instruction set per issue. A new holder must explicitly
-- acknowledge it before another structured yield can replace the context.
CREATE UNIQUE INDEX idx_issue_claim_handoffs_one_open
    ON issue_claim_handoffs (issue_id)
    WHERE resume_event_id IS NULL;

CREATE INDEX idx_issue_claim_handoffs_history
    ON issue_claim_handoffs (issue_id, yield_event_id DESC);

-- Bind authoritative handoffs only to native, unrestricted yield audit events.
CREATE TRIGGER issue_claim_handoff_yield_event_required
BEFORE INSERT ON issue_claim_handoffs
WHEN NOT EXISTS (
    SELECT 1
      FROM activity AS a
      JOIN issue_leases AS lease
        ON lease.issue_id = NEW.issue_id
       AND lease.holder_id = a.actor_id
       AND lease.generation = NEW.lease_generation
     WHERE a.id = NEW.yield_event_id
       AND a.target_kind = 'issue'
       AND a.target_id = NEW.issue_id
       AND a.verb = 'claim_yielded'
       AND a.detail = 'generation ' || NEW.lease_generation ||
           '; handoff ' || NEW.handoff_token || '; ' || NEW.reason ||
           ': ' || substr(NEW.blocking_question, 1, 200)
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
       AND lease.expires_at > datetime('now')
)
BEGIN
    SELECT RAISE(ABORT, 'matching native claim yield event required');
END;

CREATE TRIGGER issue_claim_handoff_evidence_shape_required
BEFORE INSERT ON issue_claim_handoffs
WHEN json_valid(NEW.evidence_json)
 AND json_type(NEW.evidence_json) = 'array'
 AND (
     json_array_length(NEW.evidence_json) > 10
     OR EXISTS (
         SELECT 1 FROM json_each(NEW.evidence_json) AS item
          WHERE item.type <> 'text'
             OR length(trim(
                    item.value,
                    ' ' || char(9) || char(10) || char(13)
                )) = 0
             OR length(item.value) > 1000
     )
 )
BEGIN
    SELECT RAISE(ABORT, 'valid claim handoff evidence required');
END;

CREATE TRIGGER issue_claim_handoff_must_start_open
BEFORE INSERT ON issue_claim_handoffs
WHEN NEW.resume_event_id IS NOT NULL
  OR NEW.resume_lease_generation IS NOT NULL
  OR NEW.resume_note IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'claim handoff must start open');
END;

CREATE TRIGGER issue_claim_handoff_resume_event_required
BEFORE UPDATE OF resume_event_id, resume_lease_generation, resume_note
    ON issue_claim_handoffs
WHEN NEW.resume_event_id IS NULL OR NOT EXISTS (
    SELECT 1
      FROM activity AS a
      JOIN issue_leases AS lease
        ON lease.issue_id = NEW.issue_id
       AND lease.holder_id = a.actor_id
       AND lease.generation = NEW.resume_lease_generation
     WHERE a.id = NEW.resume_event_id
       AND a.target_kind = 'issue'
       AND a.target_id = NEW.issue_id
       AND a.verb = 'claim_handoff_resumed'
       AND a.detail = 'handoff ' || NEW.handoff_token ||
           '; generation ' || NEW.resume_lease_generation
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
       AND a.id > NEW.yield_event_id
       AND lease.expires_at > datetime('now')
)
BEGIN
    SELECT RAISE(ABORT, 'matching native handoff resume event required');
END;

-- Core instructions are immutable. The only update is one atomic open -> resumed
-- transition; a resumed record cannot be rewritten or reopened.
CREATE TRIGGER issue_claim_handoff_transition_only
BEFORE UPDATE ON issue_claim_handoffs
WHEN NEW.id IS NOT OLD.id
  OR NEW.handoff_token IS NOT OLD.handoff_token
  OR NEW.issue_id IS NOT OLD.issue_id
  OR NEW.yield_event_id IS NOT OLD.yield_event_id
  OR NEW.lease_generation IS NOT OLD.lease_generation
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.reason IS NOT OLD.reason
  OR NEW.note IS NOT OLD.note
  OR NEW.attempted_work IS NOT OLD.attempted_work
  OR NEW.evidence_json IS NOT OLD.evidence_json
  OR NEW.blocking_question IS NOT OLD.blocking_question
  OR NEW.resume_instructions IS NOT OLD.resume_instructions
  OR OLD.resume_event_id IS NOT NULL
  OR NEW.resume_event_id IS NULL
  OR NEW.resume_lease_generation IS NULL
BEGIN
    SELECT RAISE(ABORT, 'claim handoff transition is invalid');
END;

CREATE TRIGGER issue_claim_handoffs_no_delete
BEFORE DELETE ON issue_claim_handoffs
BEGIN
    SELECT RAISE(ABORT, 'claim handoffs are durable');
END;

-- Once an activity row supplies a handoff's actor/generation/payload provenance,
-- protect that evidence even from raw SQL mutation. The general activity API is
-- append-only by convention; these operationally authoritative references also
-- enforce it in the database.
CREATE TRIGGER issue_claim_handoff_activity_no_update
BEFORE UPDATE ON activity
WHEN EXISTS (
    SELECT 1 FROM issue_claim_handoffs h
     WHERE h.yield_event_id = OLD.id OR h.resume_event_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'claim handoff activity is immutable');
END;

CREATE TRIGGER issue_claim_handoff_activity_no_delete
BEFORE DELETE ON activity
WHEN EXISTS (
    SELECT 1 FROM issue_claim_handoffs h
     WHERE h.yield_event_id = OLD.id OR h.resume_event_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'claim handoff activity is immutable');
END;
