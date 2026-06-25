-- 0019_user_roles: coarse user roles for administration and read-only viewers.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it.

ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'
    CHECK (role IN ('admin', 'member', 'viewer'));

-- Preserve an administrator on upgraded installs. New installs also get the
-- first user promoted by the users API, but migrations may run against existing
-- data that pre-dates the role column.
UPDATE users
SET role = 'admin'
WHERE id = (SELECT MIN(id) FROM users);
