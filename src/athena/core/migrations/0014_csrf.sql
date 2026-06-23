-- 0014_csrf: per-session CSRF token for the browser write path.
-- FORWARD-ONLY: once applied anywhere, never edit this file; add 0015_*.sql.

-- The session cookie is SameSite=Lax, which already blocks most cross-site form
-- POSTs from carrying it — but that is a browser-dependent, partial defense. The
-- synchronizer-token pattern closes the gap: every browser session also gets an
-- unguessable CSRF token, embedded in each form we render and required back on
-- every state-changing POST. A cross-site page can make the victim's browser
-- send a request, but it cannot READ this token (same-origin policy), so it
-- cannot forge a valid one.
--
-- DEFAULT '' is only for the few sessions that predate this column; an empty
-- token never validates (the server rejects a blank/absent token), so those
-- sessions simply have to re-submit from a freshly rendered form.
ALTER TABLE sessions ADD COLUMN csrf_token TEXT NOT NULL DEFAULT '';
