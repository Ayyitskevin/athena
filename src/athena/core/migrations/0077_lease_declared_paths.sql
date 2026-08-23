-- 0077_lease_declared_paths: optional file fence on an issue lease.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0078_*.sql.
--
-- A lease already stops two agents holding the same issue. It does not stop two
-- agents holding different issues and writing the same file. declared_paths is
-- an optional, holder-declared list of repo-relative POSIX paths. Empty means
-- "issue fence only" — the pre-0077 behavior. Overlap policy lives in the
-- command, not here.

ALTER TABLE issue_leases ADD COLUMN declared_paths TEXT NOT NULL DEFAULT '[]';
