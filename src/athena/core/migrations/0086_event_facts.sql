-- 0086_event_facts: an event's lease generation, handoff token and verdict as
-- fields, and the indexes the event feed's `mine` filter reads (spec F8).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0087_*.sql.
--
-- The lease events carry their generation, a yield its handoff token and a
-- completion its reported verdict only inside the prose `detail` string, so an
-- agent waiting on the feed had to parse prose to learn them (FLEET-7). The
-- recorder that writes that prose now writes the same values here, from the
-- same arguments, in the same transaction: one row per event that carries at
-- least one of them, keyed 1:1 to the event, immutable. `detail` is unchanged.
--
-- A side table, not columns on `activity`, for three reasons:
--   * the trail's hash chain (0072) covers the stored columns of an activity
--     row under a pinned recipe (schema_version = 1). A new column would be a
--     stored fact the chain does not cover, or a recipe change for every row;
--     a side row leaves the chained row exactly as it is;
--   * most verbs carry none of these facts. The table holds a row only for the
--     lease and completion events, as 0055's lifecycle facts and 0068's
--     assignee facts hold rows only for theirs;
--   * it follows those two tables: typed, 1:1 with a native event, written by
--     the recorder in the event's transaction, refused for imported history.
--
-- The side row is not attested: the hash chain covers the activity row, whose
-- `detail` names the same values, and not this table. The triggers below make
-- a facts row append-only; a verifier that needs the attested value reads
-- `detail`.
--
-- No backfill: an event recorded before this migration has no row and its
-- fields read as absent; its `detail` still carries the prose. Backfilling
-- would mean parsing that prose, which is what this table exists to avoid.
CREATE TABLE activity_event_facts (
    event_id      INTEGER PRIMARY KEY REFERENCES activity(id),
    generation    TEXT CHECK (
                      generation IS NULL OR (
                          length(generation) = 32 AND
                          generation NOT GLOB '*[^0-9a-f]*'
                      )
                  ),
    handoff_token TEXT CHECK (
                      handoff_token IS NULL OR (
                          length(handoff_token) = 32 AND
                          handoff_token NOT GLOB '*[^0-9a-f]*'
                      )
                  ),
    verdict       TEXT CHECK (
                      verdict IS NULL OR
                      verdict IN ('PASS', 'PASS-WITH-NOTES', 'HOLD', 'BLOCKED')
                  ),
    CHECK (generation IS NOT NULL OR handoff_token IS NOT NULL OR verdict IS NOT NULL)
);

-- Facts belong to native history only: an imported row is something Athena
-- was told, and must never acquire typed lease facts.
CREATE TRIGGER activity_event_facts_native_event_required
BEFORE INSERT ON activity_event_facts
WHEN NOT EXISTS (
    SELECT 1 FROM activity AS a
     WHERE a.id = NEW.event_id AND a.imported_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'native activity event required');
END;

CREATE TRIGGER activity_event_facts_immutable_update
BEFORE UPDATE ON activity_event_facts
BEGIN
    SELECT RAISE(ABORT, 'activity event facts are append-only');
END;

CREATE TRIGGER activity_event_facts_immutable_delete
BEFORE DELETE ON activity_event_facts
BEGIN
    SELECT RAISE(ABORT, 'activity event facts are append-only');
END;

-- `GET /events?mine=true` reads the issues an account is assigned, delegated or
-- created, and those it delegated to another account. 0045 indexed the
-- assignee; these three index the others, so the filter seeks instead of
-- scanning every issue or every delegation.
CREATE INDEX idx_issues_created_by ON issues (created_by);
CREATE INDEX idx_issue_contributors_user ON issue_contributors (user_id, issue_id);
CREATE INDEX idx_issue_contributors_added_by ON issue_contributors (added_by, issue_id);
