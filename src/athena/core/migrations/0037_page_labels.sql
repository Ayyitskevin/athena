-- 0037_page_labels: attach the shared label vocabulary to Mentor pages.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0038_*.sql.
--
-- The page twin of 0007's issue_labels. Labels are shared vocabulary (the `labels`
-- table, now owned by core); this is the page<->label pairing, parallel to the
-- issue<->label one, so a page can carry the SAME labels an issue does.

CREATE TABLE page_labels (
    page_id  INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (page_id, label_id)
);
