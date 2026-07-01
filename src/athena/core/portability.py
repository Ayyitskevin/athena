"""Selective JSON export bundles for Athena portability.

The V1 bundle is export-only: it captures one project or one space plus the rows
needed to understand that container outside the live database. It intentionally
does not include secrets, token hashes, session state, OIDC state, idempotency
records, webhook configuration, or attachment blob paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable

SCHEMA = "athena.portability.v1"

_PROJECT_COLS = (
    "id, name, key, description, created_by, created_at, issue_counter, visibility"
)
_SPACE_COLS = "id, key, name, description, created_by, created_at, visibility"
_USER_COLS = "id, email, name, role, is_agent, created_at"
_ISSUE_COLS = (
    "id, title, body, status, priority, created_by, created_at, assignee_id, "
    "project_id, project_seq, parent_id, sprint_id, archived_at"
)
_PAGE_COLS = (
    "id, space_id, parent_id, title, body, created_by, created_at, updated_by, updated_at"
)
_PAGE_VERSION_COLS = "id, page_id, version, title, body, edited_by, created_at"
_ATTACHMENT_COLS = (
    "id, target_kind, target_id, filename, content_type, byte_size, sha256, "
    "uploaded_by, created_at"
)
_ACTIVITY_COLS = (
    "id, actor_id, verb, target_kind, target_id, detail, created_at, "
    "run_id, parent_run_id, forked_from_event_id"
)


def export_database(
    db_path: str | Path,
    kind: str,
    target_id: int,
    bundle_path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Write a selective export bundle to ``bundle_path`` and return that path."""
    database = Path(db_path)
    destination = Path(bundle_path)
    if not database.exists():
        raise FileNotFoundError(f"database path does not exist: {database}")
    if not database.is_file():
        raise FileNotFoundError(f"database path is not a file: {database}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"export path already exists: {destination}")

    conn = _connect_readonly(database)
    try:
        bundle = export_bundle(conn, kind, target_id)
    finally:
        conn.close()

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def export_bundle(conn: sqlite3.Connection, kind: str, target_id: int) -> dict:
    """Return the in-memory export bundle for one project or space."""
    if kind == "project":
        return _project_bundle(conn, target_id)
    if kind == "space":
        return _space_bundle(conn, target_id)
    raise ValueError("export kind must be 'project' or 'space'")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _project_bundle(conn: sqlite3.Connection, project_id: int) -> dict:
    project = _one(
        conn,
        f"SELECT {_PROJECT_COLS} FROM projects WHERE id = ?",
        (project_id,),
    )
    if project is None:
        raise ValueError(f"no such project: {project_id}")

    issues = _rows(
        conn,
        f"SELECT {_ISSUE_COLS} FROM issues WHERE project_id = ? ORDER BY id",
        (project_id,),
    )
    issue_ids = [issue["id"] for issue in issues]

    users = _UserCollector()
    users.add(project["created_by"])
    for issue in issues:
        users.add(issue["created_by"], issue["assignee_id"])

    comments = _rows_for_ids(
        conn,
        issue_ids,
        "SELECT id, issue_id, author_id, body, created_at "
        "FROM comments WHERE issue_id IN ({}) ORDER BY issue_id, id",
    )
    for comment in comments:
        users.add(comment["author_id"])

    contributors = _rows_for_ids(
        conn,
        issue_ids,
        "SELECT issue_id, user_id, added_by, added_at "
        "FROM issue_contributors WHERE issue_id IN ({}) ORDER BY issue_id, user_id",
    )
    for contributor in contributors:
        users.add(contributor["user_id"], contributor["added_by"])

    issue_links = _issue_links(conn, issue_ids)
    for link in issue_links:
        users.add(link["created_by"])

    label_links = _rows_for_ids(
        conn,
        issue_ids,
        "SELECT issue_id, label_id FROM issue_labels "
        "WHERE issue_id IN ({}) ORDER BY issue_id, label_id",
    )
    labels = _labels_for_links(conn, [row["label_id"] for row in label_links])

    attachments = _attachments(conn, "issue", issue_ids)
    for attachment in attachments:
        users.add(attachment["uploaded_by"])

    activity = _activity_for_targets(conn, {"project": [project_id], "issue": issue_ids})
    for event in activity:
        users.add(event["actor_id"])

    members = _rows(
        conn,
        "SELECT project_id, user_id, added_by, added_at "
        "FROM project_members WHERE project_id = ? ORDER BY user_id",
        (project_id,),
    )
    for member in members:
        users.add(member["user_id"], member["added_by"])

    return _base_bundle("project", project_id) | {
        "project": project,
        "statuses": _rows(
            conn,
            "SELECT id, project_id, name, category, position "
            "FROM project_statuses WHERE project_id = ? ORDER BY position, id",
            (project_id,),
        ),
        "members": members,
        "sprints": _rows(
            conn,
            "SELECT id, project_id, name, goal, state, start_date, end_date, created_at "
            "FROM sprints WHERE project_id = ? ORDER BY id",
            (project_id,),
        ),
        "issues": issues,
        "comments": comments,
        "contributors": contributors,
        "issue_links": issue_links,
        "labels": labels,
        "label_links": label_links,
        "attachments": attachments,
        "cross_links": _cross_links(conn, "issue", issue_ids),
        "activity": activity,
        "users": _users(conn, users.ids),
    }


def _space_bundle(conn: sqlite3.Connection, space_id: int) -> dict:
    space = _one(
        conn,
        f"SELECT {_SPACE_COLS} FROM spaces WHERE id = ?",
        (space_id,),
    )
    if space is None:
        raise ValueError(f"no such space: {space_id}")

    pages = _rows(
        conn,
        f"SELECT {_PAGE_COLS} FROM pages WHERE space_id = ? ORDER BY id",
        (space_id,),
    )
    page_ids = [page["id"] for page in pages]

    users = _UserCollector()
    users.add(space["created_by"])
    for page in pages:
        users.add(page["created_by"], page["updated_by"])

    versions = _rows_for_ids(
        conn,
        page_ids,
        f"SELECT {_PAGE_VERSION_COLS} FROM page_versions "
        "WHERE page_id IN ({}) ORDER BY page_id, version",
    )
    for version in versions:
        users.add(version["edited_by"])

    comments = _rows_for_ids(
        conn,
        page_ids,
        "SELECT id, page_id, author_id, body, created_at "
        "FROM page_comments WHERE page_id IN ({}) ORDER BY page_id, id",
    )
    for comment in comments:
        users.add(comment["author_id"])

    label_links = _rows_for_ids(
        conn,
        page_ids,
        "SELECT page_id, label_id FROM page_labels "
        "WHERE page_id IN ({}) ORDER BY page_id, label_id",
    )
    labels = _labels_for_links(conn, [row["label_id"] for row in label_links])

    attachments = _attachments(conn, "page", page_ids)
    for attachment in attachments:
        users.add(attachment["uploaded_by"])

    activity = _activity_for_targets(conn, {"space": [space_id], "page": page_ids})
    for event in activity:
        users.add(event["actor_id"])

    members = _rows(
        conn,
        "SELECT space_id, user_id, added_by, added_at "
        "FROM space_members WHERE space_id = ? ORDER BY user_id",
        (space_id,),
    )
    for member in members:
        users.add(member["user_id"], member["added_by"])

    return _base_bundle("space", space_id) | {
        "space": space,
        "members": members,
        "pages": pages,
        "versions": versions,
        "comments": comments,
        "labels": labels,
        "label_links": label_links,
        "attachments": attachments,
        "cross_links": _cross_links(conn, "page", page_ids),
        "activity": activity,
        "users": _users(conn, users.ids),
    }


def _base_bundle(kind: str, target_id: int) -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "kind": kind,
        "root_id": target_id,
        "exported_at": _utc_now(),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _one(
    conn: sqlite3.Connection, sql: str, params: Iterable = ()
) -> dict | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return _dict(row) if row else None


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[dict]:
    return [_dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _rows_for_ids(
    conn: sqlite3.Connection,
    ids: list[int],
    sql_template: str,
) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return _rows(conn, sql_template.format(placeholders), ids)


def _dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    if "is_agent" in out:
        out["is_agent"] = bool(out["is_agent"])
    return out


def _labels_for_links(conn: sqlite3.Connection, label_ids: list[int]) -> list[dict]:
    ids = sorted(set(label_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return _rows(
        conn,
        f"SELECT id, name, color FROM labels WHERE id IN ({placeholders}) "
        "ORDER BY name COLLATE NOCASE, id",
        ids,
    )


def _attachments(
    conn: sqlite3.Connection, target_kind: str, target_ids: list[int]
) -> list[dict]:
    if not target_ids:
        return []
    placeholders = ",".join("?" for _ in target_ids)
    return _rows(
        conn,
        f"SELECT {_ATTACHMENT_COLS} FROM attachments "
        f"WHERE target_kind = ? AND target_id IN ({placeholders}) "
        "ORDER BY target_kind, target_id, id",
        (target_kind, *target_ids),
    )


def _cross_links(
    conn: sqlite3.Connection, bundle_source_kind: str, source_ids: list[int]
) -> list[dict]:
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    return _rows(
        conn,
        "SELECT source_kind, source_id, target_kind, target_id FROM links "
        f"WHERE (source_kind = ? AND source_id IN ({placeholders})) "
        f"OR (target_kind = ? AND target_id IN ({placeholders})) "
        "ORDER BY source_kind, source_id, target_kind, target_id",
        (bundle_source_kind, *source_ids, bundle_source_kind, *source_ids),
    )


def _issue_links(conn: sqlite3.Connection, issue_ids: list[int]) -> list[dict]:
    if not issue_ids:
        return []
    placeholders = ",".join("?" for _ in issue_ids)
    return _rows(
        conn,
        "SELECT from_id, to_id, kind, created_by, created_at FROM issue_links "
        f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders}) "
        "ORDER BY from_id, to_id, kind",
        (*issue_ids, *issue_ids),
    )


def _activity_for_targets(
    conn: sqlite3.Connection, targets: dict[str, list[int]]
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    for kind in sorted(targets):
        ids = targets[kind]
        if not ids:
            continue
        placeholders = ",".join("?" for _ in ids)
        clauses.append(f"(target_kind = ? AND target_id IN ({placeholders}))")
        params.extend([kind, *ids])
    if not clauses:
        return []
    return _rows(
        conn,
        f"SELECT {_ACTIVITY_COLS} FROM activity "
        f"WHERE {' OR '.join(clauses)} ORDER BY id",
        params,
    )


def _users(conn: sqlite3.Connection, user_ids: set[int]) -> list[dict]:
    ids = sorted(user_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return _rows(
        conn,
        f"SELECT {_USER_COLS} FROM users WHERE id IN ({placeholders}) ORDER BY id",
        ids,
    )


class _UserCollector:
    def __init__(self) -> None:
        self.ids: set[int] = set()

    def add(self, *user_ids: int | None) -> None:
        for user_id in user_ids:
            if user_id is not None:
                self.ids.add(user_id)
