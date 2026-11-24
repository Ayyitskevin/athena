-- issue_prior_texts: the title and body an issue edit replaced, kept beside the
-- edit's event, so the old text can be read and the edit undone (spec F10, D20).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add a new one.
--
-- Before this table an edit of an issue's title or body kept the new title in
-- the `issue_edited` event's `detail` and nothing else: the replaced text was in
-- no event, no history and no undo (FLEET-10), while the conductor recipe keeps
-- an outcome's contract and acceptance criteria in issue bodies. The recorder
-- that writes `issue_edited` (aegis/issue_activity.record_edited) now writes one
-- row here, in the same transaction: the prior text of each field the edit
-- changed, and a SHA-256 of the text the edit left, so undo can tell whether
-- that text is still the issue's own before it restores the prior one.
--
-- A side table keyed 1:1 to the event, like the lifecycle, assignee and event
-- facts tables, and not text inside `detail`:
--   * the trail's hash chain covers `detail`, and bodies are large; the chained
--     row stays exactly as it was;
--   * the feed, notifications, the desk and the work-context packet read
--     `activity`, and none of them grows by the size of a body. Only the read
--     that asks for the prior text (GET /issues/{id}/prior-texts/{event_id})
--     and undo join this table.
--
-- A field the edit did not change has NULL prior text and NULL hash. An edit
-- that changed nothing records no event, so it stores nothing here either, and
-- every row names at least one changed field.
--
-- Retention: no row is ever removed. Each edit stores the replaced text of the
-- fields it changed plus a 64-character hash per changed field.
--
-- The row is not attested: the chain covers the activity row and not this
-- table. The triggers below make a row append-only.
--
-- No backfill: an edit recorded before this table has no row; its old text was
-- never stored and cannot be recovered. Imported history acquires no row: the
-- trigger refuses one for an imported event, and the import path writes none.
CREATE TABLE issue_prior_texts (
    event_id          INTEGER PRIMARY KEY REFERENCES activity(id),
    issue_id          INTEGER NOT NULL CHECK (issue_id > 0),
    prior_title       TEXT,
    prior_body        TEXT,
    after_title_sha256 TEXT CHECK (
                          after_title_sha256 IS NULL OR (
                              length(after_title_sha256) = 64 AND
                              after_title_sha256 NOT GLOB '*[^0-9a-f]*'
                          )
                      ),
    after_body_sha256 TEXT CHECK (
                          after_body_sha256 IS NULL OR (
                              length(after_body_sha256) = 64 AND
                              after_body_sha256 NOT GLOB '*[^0-9a-f]*'
                          )
                      ),
    CHECK ((prior_title IS NULL) = (after_title_sha256 IS NULL)),
    CHECK ((prior_body IS NULL) = (after_body_sha256 IS NULL)),
    CHECK (prior_title IS NOT NULL OR prior_body IS NOT NULL)
);

-- A row belongs to the native `issue_edited` event of the same issue only.
CREATE TRIGGER issue_prior_texts_event_required
BEFORE INSERT ON issue_prior_texts
WHEN NOT EXISTS (
    SELECT 1
      FROM activity AS a
     WHERE a.id = NEW.event_id
       AND a.verb = 'issue_edited'
       AND a.target_kind = 'issue'
       AND a.target_id = NEW.issue_id
       AND a.imported_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'matching native issue_edited event required');
END;

CREATE TRIGGER issue_prior_texts_immutable_update
BEFORE UPDATE ON issue_prior_texts
BEGIN
    SELECT RAISE(ABORT, 'issue prior texts are append-only');
END;

CREATE TRIGGER issue_prior_texts_immutable_delete
BEFORE DELETE ON issue_prior_texts
BEGIN
    SELECT RAISE(ABORT, 'issue prior texts are append-only');
END;
