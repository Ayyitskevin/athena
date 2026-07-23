-- 0060_project_blocked_close_policy: optional project governance for agent closes.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it -- add 0061.
--
-- Existing projects keep today's behavior.  An operator may opt a project into the
-- stricter rule later; issue commands read this durable flag on every relevant write.
ALTER TABLE projects
    ADD COLUMN block_agent_closes_when_blocked INTEGER NOT NULL DEFAULT 0
    CHECK (block_agent_closes_when_blocked IN (0, 1));
