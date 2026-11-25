-- 0093_fleet_seats: the declared fleet seats move from source to data
-- (requirement U4, decision D27).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add a new one.
--
-- The Fleet roster and "Assign to a seat" knew exactly eight seats, written
-- into core/fleet_roster.py as DECLARED_SEATS. A new instance opened its Fleet
-- page on a "Roster drift" alert about seats it never had, every agent onboarded
-- through the cockpit was listed as drift, and nothing could declare a seat.
--
-- One row per seat, carrying what DECLARED_SEATS carried and nothing secret:
-- public Buzz identities and unit names. The slug is the stable key agents and
-- the cockpit use (Buzz, systemd, the checkout branch, the desk's seat_slug),
-- so it is unique, lower case and never edited. An email belongs to at most one
-- seat: it is how an Athena account is matched to its seat. fleet_seat_commands
-- owns the writes (declare, edit, remove) and records each on the trail.
--
-- An existing instance keeps its eight; a new one starts with none (D27). A
-- migration runs on both, so the seed is told apart by what the database
-- already holds: a database being bootstrapped has no user yet when its
-- migrations run, and an instance in use has at least one. The eight are copied
-- as DECLARED_SEATS read on the day it was removed.
CREATE TABLE fleet_seats (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE
        CHECK (length(slug) BETWEEN 1 AND 40
               AND slug NOT GLOB '*[^a-z0-9-]*'
               AND substr(slug, 1, 1) != '-'),
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    email       TEXT UNIQUE
        CHECK (email IS NULL
               OR (length(email) BETWEEN 3 AND 254 AND email = lower(email))),
    unit        TEXT
        CHECK (unit IS NULL
               OR (length(unit) BETWEEN 1 AND 200 AND substr(unit, 1, 1) != '-')),
    -- Which systemd manager owns the unit: cloud seats run as user units; the
    -- hardened local workers run as system units under service accounts.
    unit_scope  TEXT NOT NULL DEFAULT 'user'
        CHECK (unit_scope IN ('user', 'system')),
    buzz_pubkey TEXT
        CHECK (buzz_pubkey IS NULL
               OR (length(buzz_pubkey) = 64
                   AND buzz_pubkey NOT GLOB '*[^0-9a-f]*')),
    kind        TEXT NOT NULL CHECK (kind IN ('operator', 'seat', 'local_ollama')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO fleet_seats (slug, name, email, unit, unit_scope, buzz_pubkey, kind)
SELECT seed.slug, seed.name, seed.email, seed.unit, seed.unit_scope,
       seed.buzz_pubkey, seed.kind
FROM (
    SELECT 1 AS ord, 'kevin' AS slug, 'Kevin' AS name,
           'kevin@athena.local' AS email, NULL AS unit, 'user' AS unit_scope,
           '2bee90a820486a2b2e8ea97e8b058f4201cd6eac6ac15e98d9727bccd8f6f580'
               AS buzz_pubkey,
           'operator' AS kind
    UNION ALL SELECT 2, 'claude', 'Claude', 'claude@agents.local',
           'buzz-acp-claude.service', 'user',
           'c55e70289ba2cb6f5acb2ab0c9c39a6f20d23000ff57c58d568193dbaab20041',
           'seat'
    UNION ALL SELECT 3, 'codex', 'Codex', 'codex@agents.local',
           'buzz-acp-codex.service', 'user',
           'c92269929d4b5d48282f39858a43d90737edfce526bf6a3a6899b5e6f5abe3f8',
           'seat'
    UNION ALL SELECT 4, 'kimi', 'Kimi', 'kimi@agents.local',
           'buzz-acp-kimi.service', 'user',
           '537d7a475446bb89f0429068a7810f8f9d550371e7ab6be5ae89b3b94b75220e',
           'seat'
    UNION ALL SELECT 5, 'grok', 'Grok', 'grok@agents.local',
           'buzz-acp-grok.service', 'user',
           'f6f7c025959f0a215184e8e92e623dd5a798f86024df191c00c1674fbb191c8e',
           'seat'
    UNION ALL SELECT 6, 'glm', 'GLM', NULL,
           'buzz-acp-glm.service', 'user',
           '6c390afaff47b4aaacda404a60694fae43e86f071c15d7eae3abf5fe009fdc6f',
           'seat'
    UNION ALL SELECT 7, 'muse', 'Muse', 'muse@agents.local',
           'buzz-seat-muse.service', 'system',
           'a6466cb725ec90e9c759317df1bb5120c951474b7cf62ae5cb84f5bdeae98aa0',
           'local_ollama'
    UNION ALL SELECT 8, 'qwen', 'Qwen', 'qwen@agents.local',
           'buzz-seat-qwen.service', 'system',
           '7c29f9b39e7cb588617d9ed694ad2ed591aa77006d613e6b8b25a660f70f8020',
           'local_ollama'
) AS seed
WHERE EXISTS (SELECT 1 FROM users)
ORDER BY seed.ord;
