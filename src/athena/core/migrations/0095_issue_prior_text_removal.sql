-- issue_prior_texts: an administrator may remove one kept prior text, and only
-- with its audit event (decision 6 of docs/decisions/2026-11-26-owner-decisions.md).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add a new one.
--
-- 0089 made a row append-only: no UPDATE, no DELETE. That made an edit unable to
-- remove text: editing a secret out of an issue left it readable to everyone who
-- could see the edit. A removal is now possible and still never silent. The
-- delete trigger is replaced by one that lets a row go only when the trail
-- already holds the native `issue_prior_text_removed` event naming it, recorded
-- by the same command in the same transaction (aegis/prior_text_commands.py).
-- The event says who removed which edit's text and when, never the text.
--
-- UPDATE stays refused: a kept text is read whole or removed whole, never
-- rewritten. Removing a row also makes that edit's undo refuse, as an edit
-- recorded before 0089 always has; the issue's current text is untouched.
DROP TRIGGER issue_prior_texts_immutable_delete;

CREATE TRIGGER issue_prior_texts_delete_requires_removal_event
BEFORE DELETE ON issue_prior_texts
WHEN NOT EXISTS (
    SELECT 1
      FROM activity AS a
     WHERE a.verb = 'issue_prior_text_removed'
       AND a.target_kind = 'issue'
       AND a.target_id = OLD.issue_id
       AND a.detail = 'edit ' || OLD.event_id
       AND a.imported_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'a prior text is removed only with its removal event');
END;
