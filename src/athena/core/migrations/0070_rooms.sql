-- 0070_rooms: project-scoped coordination rooms over Athena's authoritative
-- records. Room prose stays on the append-only activity row; room_events is the
-- typed, immutable 1:1 metadata extension for that row.
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0071_*.sql.

-- Room coordination is deliberately internal. Existing activity remains eligible
-- for automation and outbound delivery; room commands explicitly write zero.
ALTER TABLE activity ADD COLUMN delivery_eligible INTEGER NOT NULL DEFAULT 1
    CHECK (delivery_eligible IN (0, 1));

-- A room keeps both the project row id and its immutable activity generation key.
-- There is intentionally no FK to projects: projects may be hard-deleted once
-- empty, while durable room/audit history must neither block that delete nor bind
-- to a later project that reuses the integer id. Live reads join on BOTH values.
CREATE TABLE rooms (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        INTEGER NOT NULL CHECK (project_id > 0),
    project_scope_key TEXT NOT NULL CHECK (
                          length(project_scope_key) = 32 AND
                          project_scope_key NOT GLOB '*[^0-9a-f]*'
                      ),
    slug              TEXT NOT NULL COLLATE NOCASE CHECK (
                          length(slug) BETWEEN 1 AND 80 AND
                          slug = lower(slug) AND
                          slug NOT GLOB '*[^a-z0-9-]*' AND
                          slug NOT LIKE '-%' AND
                          slug NOT LIKE '%-' AND
                          slug NOT LIKE '%--%'
                      ),
    room_type         TEXT NOT NULL CHECK (
                          room_type IN ('project', 'work_item', 'agent', 'brief')
                      ),
    title             TEXT NOT NULL CHECK (
                          length(trim(title, ' ' || char(9) || char(10) || char(13)))
                              BETWEEN 1 AND 300 AND
                          length(title) <= 300
                      ),
    purpose           TEXT NOT NULL DEFAULT '' CHECK (length(purpose) <= 4000),
    visibility        TEXT NOT NULL DEFAULT 'members'
                          CHECK (visibility IN ('project', 'members')),
    -- Link ids are historical coordinates. Insert/update triggers require the
    -- matching live row and project at creation time without making room history
    -- cascade or block a later issue/user lifecycle change.
    issue_id          INTEGER CHECK (issue_id IS NULL OR issue_id > 0),
    agent_id          INTEGER CHECK (agent_id IS NULL OR agent_id > 0),
    created_by        INTEGER NOT NULL REFERENCES users(id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at       TEXT,
    UNIQUE (project_scope_key, slug),
    CHECK (
        (room_type IN ('project', 'brief') AND issue_id IS NULL AND agent_id IS NULL)
        OR (room_type = 'work_item' AND issue_id IS NOT NULL AND agent_id IS NULL)
        OR (room_type = 'agent' AND issue_id IS NULL AND agent_id IS NOT NULL)
    ),
    CHECK (room_type <> 'project' OR slug = 'main'),
    CHECK (slug <> 'main' OR room_type = 'project'),
    CHECK (room_type <> 'brief' OR slug = 'brief'),
    CHECK (slug <> 'brief' OR room_type = 'brief'),
    CHECK (
        slug NOT LIKE 'agent-%'
        OR (room_type = 'agent' AND slug = ('agent-' || agent_id))
    ),
    CHECK (
        slug NOT LIKE 'work-item-%'
        OR (room_type = 'work_item' AND slug = ('work-item-' || issue_id))
    )
);

-- One durable default or linked room per owning record. These include archived
-- rows: archive never creates a second identity for the same project/work/agent.
CREATE UNIQUE INDEX idx_rooms_one_project_room
    ON rooms (project_scope_key) WHERE room_type = 'project';
CREATE UNIQUE INDEX idx_rooms_one_brief_room
    ON rooms (project_scope_key) WHERE room_type = 'brief';
CREATE UNIQUE INDEX idx_rooms_one_work_item_room
    ON rooms (issue_id) WHERE room_type = 'work_item';
CREATE UNIQUE INDEX idx_rooms_one_agent_room
    ON rooms (project_scope_key, agent_id) WHERE room_type = 'agent';
CREATE INDEX idx_rooms_project_list
    ON rooms (project_scope_key, archived_at, room_type, id);

CREATE TRIGGER rooms_live_project_required_insert
BEFORE INSERT ON rooms
WHEN NOT EXISTS (
    SELECT 1 FROM projects p
     WHERE p.id = NEW.project_id
       AND p.activity_scope_key = NEW.project_scope_key
)
BEGIN
    SELECT RAISE(ABORT, 'matching live room project required');
END;

CREATE TRIGGER rooms_live_work_item_required_insert
BEFORE INSERT ON rooms
WHEN NEW.room_type = 'work_item' AND NOT EXISTS (
    SELECT 1 FROM issues i
     WHERE i.id = NEW.issue_id AND i.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'matching project work item required');
END;

CREATE TRIGGER rooms_live_agent_required_insert
BEFORE INSERT ON rooms
WHEN NEW.room_type = 'agent' AND NOT EXISTS (
    SELECT 1 FROM users u WHERE u.id = NEW.agent_id AND u.is_agent = 1
)
BEGIN
    SELECT RAISE(ABORT, 'matching agent account required');
END;

-- Stable identity and linkage never change. The one exception is a work-item
-- room following its issue to another live project. Only its current ownership
-- coordinates change; old activity retains its original immutable scope envelope,
-- so a destination-only reader cannot acquire the earlier project's prose.
CREATE TRIGGER rooms_identity_immutable
BEFORE UPDATE ON rooms
WHEN NEW.id IS NOT OLD.id
  OR NEW.slug IS NOT OLD.slug
  OR NEW.room_type IS NOT OLD.room_type
  OR NEW.issue_id IS NOT OLD.issue_id
  OR NEW.agent_id IS NOT OLD.agent_id
  OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at
  OR (
      (NEW.project_id IS NOT OLD.project_id
       OR NEW.project_scope_key IS NOT OLD.project_scope_key)
      AND OLD.room_type <> 'work_item'
  )
BEGIN
    SELECT RAISE(ABORT, 'room identity is immutable');
END;

CREATE TRIGGER rooms_work_item_move_required
BEFORE UPDATE OF project_id, project_scope_key ON rooms
WHEN (NEW.project_id IS NOT OLD.project_id
      OR NEW.project_scope_key IS NOT OLD.project_scope_key)
 AND NOT EXISTS (
    SELECT 1
      FROM issues i
      JOIN projects p ON p.id = i.project_id
     WHERE NEW.room_type = 'work_item'
       AND i.id = NEW.issue_id
       AND i.project_id = NEW.project_id
       AND p.activity_scope_key = NEW.project_scope_key
)
BEGIN
    SELECT RAISE(ABORT, 'work-item room must follow its live issue project');
END;

CREATE TRIGGER rooms_invariant_rooms_not_archivable
BEFORE UPDATE OF archived_at ON rooms
WHEN NEW.archived_at IS NOT OLD.archived_at
 AND NEW.room_type IN ('project', 'brief')
BEGIN
    SELECT RAISE(ABORT, 'project and brief rooms cannot be archived');
END;

CREATE TRIGGER rooms_archive_once
BEFORE UPDATE OF archived_at ON rooms
WHEN OLD.archived_at IS NOT NULL OR NEW.archived_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'room archive is irreversible');
END;

CREATE TRIGGER rooms_no_delete
BEFORE DELETE ON rooms
BEGIN
    SELECT RAISE(ABORT, 'rooms are durable; archive instead');
END;

-- The body is activity.detail. This table owns only controlled classification,
-- a safe authoritative-record coordinate, its content digest, and supersession.
CREATE TABLE room_events (
    activity_id        INTEGER PRIMARY KEY REFERENCES activity(id),
    room_id            INTEGER NOT NULL REFERENCES rooms(id),
    event_kind         TEXT NOT NULL CHECK (
                           event_kind IN (
                               'message', 'check_in', 'handoff', 'decision',
                               'evidence', 'system_notice'
                           )
                       ),
    reference_kind     TEXT CHECK (
                           reference_kind IS NULL OR reference_kind IN (
                               'issue', 'page', 'approval', 'activity',
                               'handoff', 'dispatch', 'run', 'attachment'
                           )
                       ),
    reference_id       TEXT,
    content_sha256     TEXT NOT NULL CHECK (
                           length(content_sha256) = 64 AND
                           content_sha256 NOT GLOB '*[^0-9a-f]*'
                       ),
    supersedes_event_id INTEGER UNIQUE REFERENCES room_events(activity_id),
    CHECK (
        (reference_kind IS NULL AND reference_id IS NULL)
        OR (
            reference_kind IS NOT NULL AND reference_id IS NOT NULL AND
            typeof(reference_id) = 'text' AND
            length(trim(reference_id)) BETWEEN 1 AND 200 AND
            reference_id = trim(reference_id)
        )
    )
);

CREATE INDEX idx_room_events_room_timeline
    ON room_events (room_id, activity_id DESC);
CREATE INDEX idx_room_events_reference
    ON room_events (reference_kind, reference_id, activity_id DESC)
    WHERE reference_kind IS NOT NULL;

-- Bind metadata only to a native, fully scoped, internal-only room activity row
-- whose controlled verb agrees with its classification.
CREATE TRIGGER room_event_activity_required
BEFORE INSERT ON room_events
WHEN NOT EXISTS (
    SELECT 1 FROM activity a
     WHERE a.id = NEW.activity_id
       AND a.target_kind = 'room'
       AND a.target_id = NEW.room_id
       AND a.verb = 'room_' || NEW.event_kind
       AND a.imported_at IS NULL
       AND a.visibility_restricted = 0
       AND a.delivery_eligible = 0
       AND (SELECT COUNT(*) FROM activity_visibility_projects avp
             WHERE avp.event_id = a.id) = 1
       AND EXISTS (
           SELECT 1
             FROM activity_visibility_projects avp
             JOIN rooms scoped_room ON scoped_room.id = NEW.room_id
            WHERE avp.event_id = a.id
              AND avp.project_scope_key = scoped_room.project_scope_key
       )
       AND length(trim(
               a.detail,
               ' ' || char(9) || char(10) || char(13)
           )) > 0
       AND length(a.detail) <= 12000
)
BEGIN
    SELECT RAISE(ABORT, 'matching internal room activity required');
END;

CREATE TRIGGER room_event_supersession_required
BEFORE INSERT ON room_events
WHEN NEW.supersedes_event_id IS NOT NULL AND (
    NEW.supersedes_event_id >= NEW.activity_id OR NOT EXISTS (
        SELECT 1 FROM room_events prior
         WHERE prior.activity_id = NEW.supersedes_event_id
           AND prior.room_id = NEW.room_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'same-room prior event required for supersession');
END;

CREATE TRIGGER room_events_immutable_update
BEFORE UPDATE ON room_events
BEGIN
    SELECT RAISE(ABORT, 'room events are append-only');
END;

CREATE TRIGGER room_events_immutable_delete
BEFORE DELETE ON room_events
BEGIN
    SELECT RAISE(ABORT, 'room events are append-only');
END;

CREATE TRIGGER room_event_activity_no_update
BEFORE UPDATE ON activity
WHEN EXISTS (
    SELECT 1 FROM room_events re WHERE re.activity_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'room event activity is immutable');
END;

CREATE TRIGGER room_event_activity_no_delete
BEFORE DELETE ON activity
WHEN EXISTS (
    SELECT 1 FROM room_events re WHERE re.activity_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'room event activity is immutable');
END;

CREATE TRIGGER room_event_scope_no_insert
BEFORE INSERT ON activity_visibility_projects
WHEN EXISTS (
    SELECT 1 FROM room_events re WHERE re.activity_id = NEW.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'room event visibility scope is immutable');
END;

CREATE TRIGGER room_event_scope_no_update
BEFORE UPDATE ON activity_visibility_projects
WHEN EXISTS (
    SELECT 1 FROM room_events re WHERE re.activity_id = OLD.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'room event visibility scope is immutable');
END;

CREATE TRIGGER room_event_scope_no_delete
BEFORE DELETE ON activity_visibility_projects
WHEN EXISTS (
    SELECT 1 FROM room_events re WHERE re.activity_id = OLD.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'room event visibility scope is immutable');
END;

-- A narrowed/archived room must fence any durable idempotency response captured
-- under the previous audience before an adapter can replay its body.
CREATE TRIGGER idempotency_authz_room_visibility_or_scope
AFTER UPDATE OF visibility, project_id, project_scope_key, archived_at ON rooms
WHEN NEW.visibility IS NOT OLD.visibility
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.project_scope_key IS NOT OLD.project_scope_key
  OR NEW.archived_at IS NOT OLD.archived_at
BEGIN
    UPDATE idempotency_authorization_state
       SET revision = revision + 1 WHERE singleton = 1;
END;

-- Existing projects receive one main room and one read-only live brief.
INSERT INTO rooms (
    project_id, project_scope_key, slug, room_type, title, purpose, visibility,
    created_by, created_at, updated_at
)
SELECT
    p.id, p.activity_scope_key, 'main', 'project',
    substr(p.name, 1, 300), substr(p.description, 1, 4000), 'project',
    p.created_by, p.created_at, p.created_at
FROM projects p;

INSERT INTO rooms (
    project_id, project_scope_key, slug, room_type, title, purpose, visibility,
    created_by, created_at, updated_at
)
SELECT
    p.id, p.activity_scope_key, 'brief', 'brief',
    substr(p.name || ' live brief', 1, 300),
    'Live, read-only project coordination brief', 'project',
    p.created_by, p.created_at, p.created_at
FROM projects p;

-- Backlog issues have no project room. Every project-owned issue gets one stable
-- focused room; the global issue id makes the generated project-local slug unique.
INSERT INTO rooms (
    project_id, project_scope_key, slug, room_type, title, purpose, visibility,
    issue_id, created_by, created_at, updated_at
)
SELECT
    p.id, p.activity_scope_key, 'work-item-' || i.id, 'work_item',
    substr(i.title, 1, 300), 'Focused work-item coordination', 'project',
    i.id, i.created_by, i.created_at, i.created_at
FROM issues i
JOIN projects p ON p.id = i.project_id;


-- Backfill one agent room for every existing agent already participating in a
-- project as its creator, explicit member, issue creator/assignee, or contributor.
WITH participating_agents AS (
    SELECT p.id AS project_id, p.activity_scope_key, u.id AS agent_id,
           p.created_by, p.created_at
      FROM projects p
      JOIN users u ON u.id = p.created_by AND u.is_agent = 1
    UNION
    SELECT p.id, p.activity_scope_key, u.id, p.created_by, p.created_at
      FROM project_members pm
      JOIN projects p ON p.id = pm.project_id
      JOIN users u ON u.id = pm.user_id AND u.is_agent = 1
    UNION
    SELECT p.id, p.activity_scope_key, u.id, p.created_by, p.created_at
      FROM issues i
      JOIN projects p ON p.id = i.project_id
      JOIN users u ON u.id = i.created_by AND u.is_agent = 1
    UNION
    SELECT p.id, p.activity_scope_key, u.id, p.created_by, p.created_at
      FROM issues i
      JOIN projects p ON p.id = i.project_id
      JOIN users u ON u.id = i.assignee_id AND u.is_agent = 1
    UNION
    SELECT p.id, p.activity_scope_key, u.id, p.created_by, p.created_at
      FROM issue_contributors ic
      JOIN issues i ON i.id = ic.issue_id
      JOIN projects p ON p.id = i.project_id
      JOIN users u ON u.id = ic.user_id AND u.is_agent = 1
)
INSERT INTO rooms (
    project_id, project_scope_key, slug, room_type, title, purpose, visibility,
    agent_id, created_by, created_at, updated_at
)
SELECT
    pa.project_id, pa.activity_scope_key, 'agent-' || pa.agent_id, 'agent',
    substr(u.name, 1, 300), 'Agent coordination and visible work receipts',
    'members', pa.agent_id, pa.created_by, pa.created_at, pa.created_at
FROM participating_agents pa
JOIN users u ON u.id = pa.agent_id
ORDER BY pa.project_id, pa.agent_id;
