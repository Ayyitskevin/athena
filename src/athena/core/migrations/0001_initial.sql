-- 0001_initial: the two foundational tables.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it.
-- To change this schema later, add 0002_*.sql instead.

-- People (and, later, agents) who can act in Athena.
CREATE TABLE users (
    id         INTEGER PRIMARY KEY,            -- auto-incrementing row id
    email      TEXT NOT NULL UNIQUE,           -- no two users share an email
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The core unit of work in Aegis.
CREATE TABLE issues (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'open',
    -- every issue must point at a real user; the DB refuses orphans
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
