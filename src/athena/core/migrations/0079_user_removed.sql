-- 0079_user_removed: the lever AFTER offboarding — gone from sight, kept for history.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0080_*.sql.
--
-- Offboarding demotes and revokes but leaves the account on every roster
-- forever. Removal is the tombstone: the user vanishes from every list and
-- picker (users, agents cockpit, assignees, delegation, email lookup) and can
-- never authenticate, while every attributed row — issues, activity, forge
-- sources, attachments — keeps pointing at a real user for the load-bearing
-- audit trail. 49 foreign keys reference users; deletion would turn the trail
-- into ghosts, so removal is a state, not a DELETE. NULL = present; a
-- timestamp records when removal happened (who/why lives in the activity
-- trail via removed_user/restored_user events). Restore clears the stamp and
-- nothing else: the account returns as an offboarded viewer with no
-- credentials.

ALTER TABLE users ADD COLUMN removed_at TEXT;
