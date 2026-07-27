-- 0069_event_sources: registered inbound event sources — the forge half of the
-- integration boundary. Athena accepts signed events FROM a forge; it never calls
-- OUT to one.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0070_*.sql.
--
-- WHY a table rather than configuration: an inbound source is a CREDENTIAL plus a
-- trust decision, and both need an audited lifecycle. Who registered it, when it
-- was paused, when it was removed — those are operator-visible facts about who can
-- write foreign history into this workspace, and env-var configuration cannot
-- record them. This mirrors `webhooks` (0021), deliberately: the outbound and
-- inbound halves of the same trust boundary should be managed the same way.
--
-- WHY no stored forge credential: the secret here is OURS — the one an inbound
-- request must prove knowledge of. Athena stores no token that would let it call
-- the forge, because it never does. Inbound-only means no new egress surface and
-- no third-party credential to leak.
CREATE TABLE event_sources (
    id          INTEGER PRIMARY KEY,
    -- Operator-facing name, unique so a trail entry naming a source is unambiguous.
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    -- The payload dialect this endpoint speaks. A closed vocabulary, checked in
    -- code: an unknown kind cannot be registered, so no request can arrive for a
    -- parser that does not exist.
    kind        TEXT NOT NULL,
    -- HMAC-SHA256 secret the SENDER signs with; shown once at registration and
    -- never again, exactly like a webhook's.
    secret      TEXT NOT NULL,
    -- The host events are expected from ('github.com'). Used to decide whether a
    -- ref in the trail is a link we are willing to render — a URL is only made
    -- clickable when its host belongs to a registered source.
    host        TEXT NOT NULL,
    -- 0 stops acceptance without deleting the row (and without invalidating the
    -- secret), so an operator can cut off a noisy source and restore it.
    enabled     INTEGER NOT NULL DEFAULT 1,
    -- Health, for operator visibility. `unmatched_count` is the running number of
    -- authentic events that named no issue Athena knows: they are COUNTED, not
    -- stored, because landing them nowhere would be silent and storing them
    -- everywhere would be noise. A climbing number means the forge is busy on work
    -- this workspace does not track — a real thing to notice, and the only trace
    -- those events leave.
    accepted_count  INTEGER NOT NULL DEFAULT 0,
    unmatched_count INTEGER NOT NULL DEFAULT 0,
    last_event_at   TEXT,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Inbound delivery looks a source up by name on every request; keep it an index
-- probe. (The UNIQUE constraint above already provides this index — this comment
-- records the access pattern that justifies the constraint's shape.)
