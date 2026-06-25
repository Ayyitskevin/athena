-- 0020_token_scopes: bearer-token permissions layered below user roles.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it.

ALTER TABLE api_tokens ADD COLUMN scopes TEXT NOT NULL DEFAULT 'admin';
