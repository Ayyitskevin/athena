-- 0044_automation_rule_failures: make a failing automation rule visible.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0045.
--
-- The engine fires a rule's action best-effort (at-most-once, fire-and-forget — see
-- automation.process_pending). Until now a failing action was swallowed silently: no
-- log line, no record, so a broken rule was invisible to the operator even though the
-- audit log is meant to be load-bearing. This brings automation to parity with webhooks
-- (core/webhooks.py already tracks failure_count/last_error per subscriber): each rule
-- now carries a running failure count, the text of its most recent error, and when that
-- error happened, so the /admin console and the REST API can surface a misbehaving rule.
ALTER TABLE automation_rules ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE automation_rules ADD COLUMN last_error TEXT;
ALTER TABLE automation_rules ADD COLUMN last_error_at TEXT;
