-- 0032_oidc_login_states: short-lived state for an in-flight SSO login.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0033.
--
-- An OIDC login spans two requests with no session yet: /login/sso redirects the
-- browser to the IdP, and /auth/callback receives the result. Between them we must
-- remember three secrets generated at the start and checked at the end:
--   * state         — the unguessable value echoed back, also pinned to the browser
--                     by a cookie, so a callback can't be forged for someone else
--                     (login CSRF / fixation);
--   * nonce         — bound into the ID token, checked on return (replay defense);
--   * code_verifier — the PKCE secret proving we are the client that started.
-- Keeping them server-side (not in the cookie) means the browser never sees the
-- nonce/verifier. Rows are SINGLE-USE (deleted on callback) and short-lived (the
-- callback ignores anything older than a few minutes and sweeps stale rows), so this
-- table stays tiny — it is scratch space, not durable state.
CREATE TABLE oidc_login_states (
    state         TEXT NOT NULL PRIMARY KEY,
    nonce         TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
