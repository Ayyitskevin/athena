-- 0094_imported_text: mark the comments and pages whose current text arrived in
-- a portability bundle (requirement G3).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add a new one.
--
-- An import maps each bundle account onto the local account with the same
-- email and writes comments, pages and page versions under that account's id.
-- Their author was then read from that column, so an agent was told the local
-- account wrote text Athena was only given. Imported activity has been marked
-- since 0041 and answers authored_by null; these four tables carried nothing.
--
-- imported_at is the instant the row's CURRENT text arrived in a bundle, NULL
-- for text written here. A native edit of a comment or a page writes its own
-- text and clears it; a page version keeps the mark of the text it snapshots.
-- core/portability.py stamps it; core/authorship.py reads it.
--
-- Rows imported before this migration are not marked: no record says which
-- they were (the import is a host command with no audit event), and an author
-- is never guessed from a neighbouring fact.
ALTER TABLE comments ADD COLUMN imported_at TEXT;
ALTER TABLE page_comments ADD COLUMN imported_at TEXT;
ALTER TABLE pages ADD COLUMN imported_at TEXT;
ALTER TABLE page_versions ADD COLUMN imported_at TEXT;
