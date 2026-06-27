-- 0031_oidc_identities: link an external IdP identity to an Athena user (SSO).
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0032.
--
-- Athena's local login is email + password (users.password_hash). SSO adds a second
-- way to authenticate the SAME user rows: an OpenID Connect provider asserts an
-- identity, and this table records WHICH Athena user that external identity maps to.
--
-- An OIDC identity is the pair (issuer, subject): `iss` names the provider and `sub`
-- is the user's STABLE, opaque id within it (email can change; sub does not). The
-- composite primary key makes that pair unique, so one provider-identity maps to at
-- most one Athena user. user_id is the local account it logs into — a normal users
-- row (password-less is fine; an SSO-only user simply has no password_hash). Keeping
-- the link in its own table (rather than columns on users) means a user can carry
-- multiple linked identities and the users table stays about the account, not the
-- auth method.
CREATE TABLE oidc_identities (
    issuer     TEXT    NOT NULL,                  -- the IdP (`iss` claim)
    subject    TEXT    NOT NULL,                  -- stable user id within it (`sub` claim)
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (issuer, subject)
);

-- The hot read is "which account does this IdP identity log into" (served by the
-- PK). This index serves the reverse — "what identities are linked to this user" —
-- for an account/settings view and for cleanup when a user is managed.
CREATE INDEX idx_oidc_identities_user ON oidc_identities (user_id);
