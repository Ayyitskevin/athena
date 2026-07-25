-- 0066_issue_runbooks: bind one Mentor runbook page to the issue it is about.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0067_*.sql.
--
-- VISION's fifth step promises corrections "feed back into Mentor as durable
-- context the agents read next time". Promoting a run's learnings means appending
-- to a page, and appending means finding the SAME page again next time.
--
-- Finding it by title would work until someone renamed it, at which point the next
-- promotion would silently start a second runbook and split the memory in two.
-- That is exactly the kind of quiet divergence a knowledge base must not have, so
-- the association is stored explicitly and survives any rename.
--
-- One runbook per issue: the primary key says so. The page it names may be
-- archived or deleted like any other page — the row is a pointer, not a lifecycle
-- owner, and a promotion that finds a vanished page starts a new runbook.

CREATE TABLE issue_runbooks (
    issue_id   INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- "Which issue is this page the runbook for?" — the reverse lookup a page view
-- needs, and the one the PRIMARY KEY does not already serve.
CREATE INDEX idx_issue_runbooks_page ON issue_runbooks (page_id);
