-- 0052_page_version_run_provenance: record which agent run produced each page
-- revision, so the version history is self-describing provenance and not just a
-- cross-reference into the activity log. FORWARD-ONLY: once applied anywhere, never
-- edit this file — add 0053_*.sql.

-- The live `pages` row gains the run that last wrote it (create or edit). Stamped
-- from the request's run context on every write, exactly as the activity log stamps
-- run_id; nullable because a human write (or a pre-0052 row) has no run. This is the
-- SOURCE the snapshot copies: when the next edit supersedes this content, the run that
-- AUTHORED it travels into page_versions with the rest of the superseded revision.
ALTER TABLE pages ADD COLUMN run_id TEXT;

-- A superseded revision carries the run that produced ITS content — the page's run_id
-- at the moment the snapshot was cut, copied alongside edited_by/created_at (all three
-- describe the superseded revision, not the edit doing the superseding). Nullable: a
-- revision authored by a human, or one snapshotted from a pre-0052 page, simply has no
-- run. Existing page_versions rows keep NULL — their authoring run is genuinely unknown,
-- and inventing one would be a false provenance claim.
ALTER TABLE page_versions ADD COLUMN run_id TEXT;
