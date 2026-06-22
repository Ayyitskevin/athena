-- 0007_labels: reusable labels and the many-to-many between issues and labels.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0008_*.sql.

-- A label is a shared piece of vocabulary (e.g. "bug", "frontend"), reused
-- across many issues. name is UNIQUE and COLLATE NOCASE so "Bug" and "bug" are
-- the same label — find-or-create matches case-insensitively. color is a hex
-- string with a neutral gray default for labels created without one.
CREATE TABLE labels (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#6b7280'
);

-- The join table: one row per (issue, label) pairing. The composite PRIMARY KEY
-- makes a pairing unique (a label can't be attached to the same issue twice) and
-- indexes both directions. ON DELETE CASCADE means deleting an issue or a label
-- cleans up its pairings automatically (foreign_keys is ON per connection).
CREATE TABLE issue_labels (
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (issue_id, label_id)
);
