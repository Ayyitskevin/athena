"""Selective JSON portability bundles for Athena.

The V1 bundle captures one project or one space plus the rows needed to
understand that container outside the live database. It intentionally does not
include secrets, token hashes, session state, OIDC state, idempotency records,
webhook configuration, or attachment blob paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

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


def dry_run_import_database(db_path: str | Path, bundle_path: str | Path) -> dict:
    """Validate a portability bundle against ``db_path`` without mutating it."""
    database = Path(db_path)
    bundle_file = Path(bundle_path)
    if not database.exists():
        raise FileNotFoundError(f"database path does not exist: {database}")
    if not database.is_file():
        raise FileNotFoundError(f"database path is not a file: {database}")
    if not bundle_file.exists():
        raise FileNotFoundError(f"bundle path does not exist: {bundle_file}")
    if not bundle_file.is_file():
        raise FileNotFoundError(f"bundle path is not a file: {bundle_file}")

    bundle = _load_bundle(bundle_file)
    conn = _connect_readonly(database)
    try:
        return dry_run_import_bundle(conn, bundle)
    finally:
        conn.close()


def export_bundle(conn: sqlite3.Connection, kind: str, target_id: int) -> dict:
    """Return the in-memory export bundle for one project or space."""
    if kind == "project":
        return _project_bundle(conn, target_id)
    if kind == "space":
        return _space_bundle(conn, target_id)
    raise ValueError("export kind must be 'project' or 'space'")


def dry_run_import_bundle(conn: sqlite3.Connection, bundle: dict) -> dict:
    """Return a report for importing ``bundle`` without writing to the database."""
    bundle = _validate_bundle(bundle)
    if bundle["kind"] == "project":
        return _project_import_report(conn, bundle)
    if bundle["kind"] == "space":
        return _space_import_report(conn, bundle)
    raise ValueError("bundle kind must be 'project' or 'space'")


def _load_bundle(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"bundle is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("bundle must be a JSON object")
    return data


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


def _project_import_report(conn: sqlite3.Connection, bundle: dict) -> dict:
    _validate_project_bundle(bundle)
    report = _base_import_report(bundle)
    project = bundle["project"]
    labels = _label_import_status(conn, bundle["labels"], report)
    users = _user_import_status(conn, bundle["users"], report)

    existing_name = _one(
        conn,
        "SELECT id, name, key FROM projects WHERE name = ?",
        (project["name"],),
    )
    if existing_name is not None:
        _add_conflict(
            report,
            "project_name_exists",
            f"target database already has project name {project['name']!r}",
            path="project.name",
            value=project["name"],
            existing_id=existing_name["id"],
        )
    existing_key = _one(
        conn,
        "SELECT id, name, key FROM projects WHERE key = ?",
        (project["key"],),
    )
    if existing_key is not None:
        _add_conflict(
            report,
            "project_key_exists",
            f"target database already has project key {project['key']!r}",
            path="project.key",
            value=project["key"],
            existing_id=existing_key["id"],
        )

    issue_ids = set(_ids_from(bundle["issues"], "id", "issues"))
    _set_counts(
        report,
        would_create={
            "projects": 1,
            "project_statuses": len(bundle["statuses"]),
            "project_members": len(bundle["members"]),
            "sprints": len(bundle["sprints"]),
            "issues": len(bundle["issues"]),
            "comments": len(bundle["comments"]),
            "issue_contributors": len(bundle["contributors"]),
            "issue_links": len(bundle["issue_links"]),
            "labels": labels["missing"],
            "issue_labels": len(bundle["label_links"]),
            "attachments": len(bundle["attachments"]),
            "links": len(bundle["cross_links"]),
            "activity": len(bundle["activity"]),
        },
        would_reuse={"users": users["existing"], "labels": labels["existing"]},
    )
    _warn_external_issue_links(report, bundle["issue_links"], issue_ids)
    _warn_external_cross_links(report, bundle["cross_links"], {"issue": issue_ids})
    _warn_attachment_manifests(report, bundle["attachments"])
    return _finish_import_report(report)


def _space_import_report(conn: sqlite3.Connection, bundle: dict) -> dict:
    _validate_space_bundle(bundle)
    report = _base_import_report(bundle)
    space = bundle["space"]
    labels = _label_import_status(conn, bundle["labels"], report)
    users = _user_import_status(conn, bundle["users"], report)

    existing_key = _one(
        conn,
        "SELECT id, key, name FROM spaces WHERE key = ?",
        (space["key"],),
    )
    if existing_key is not None:
        _add_conflict(
            report,
            "space_key_exists",
            f"target database already has space key {space['key']!r}",
            path="space.key",
            value=space["key"],
            existing_id=existing_key["id"],
        )

    page_ids = set(_ids_from(bundle["pages"], "id", "pages"))
    _set_counts(
        report,
        would_create={
            "spaces": 1,
            "space_members": len(bundle["members"]),
            "pages": len(bundle["pages"]),
            "page_versions": len(bundle["versions"]),
            "page_comments": len(bundle["comments"]),
            "labels": labels["missing"],
            "page_labels": len(bundle["label_links"]),
            "attachments": len(bundle["attachments"]),
            "links": len(bundle["cross_links"]),
            "activity": len(bundle["activity"]),
        },
        would_reuse={"users": users["existing"], "labels": labels["existing"]},
    )
    _warn_external_cross_links(report, bundle["cross_links"], {"page": page_ids})
    _warn_attachment_manifests(report, bundle["attachments"])
    return _finish_import_report(report)


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


def _validate_bundle(bundle: dict) -> dict:
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be a JSON object")
    if bundle.get("schema") != SCHEMA:
        raise ValueError(
            f"unsupported bundle schema: {bundle.get('schema')!r}; expected {SCHEMA!r}"
        )
    if bundle.get("schema_version") != 1:
        raise ValueError(
            f"unsupported bundle schema_version: {bundle.get('schema_version')!r}"
        )
    kind = bundle.get("kind")
    if kind not in {"project", "space"}:
        raise ValueError("bundle kind must be 'project' or 'space'")
    if type(bundle.get("root_id")) is not int:
        raise ValueError("bundle.root_id must be an integer")
    if "exported_at" not in bundle:
        raise ValueError("bundle.exported_at is required")
    return bundle


def _validate_project_bundle(bundle: dict) -> None:
    root_id = bundle["root_id"]
    project = _require_object(bundle, "project")
    _require_fields(project, ("id", "name", "key", "created_by"), "project")
    if project["id"] != root_id:
        raise ValueError("bundle.project.id must match bundle.root_id")

    for key in (
        "statuses",
        "members",
        "sprints",
        "issues",
        "comments",
        "contributors",
        "issue_links",
        "labels",
        "label_links",
        "attachments",
        "cross_links",
        "activity",
        "users",
    ):
        _require_list(bundle, key)

    users = _validate_users(bundle["users"])
    labels = _validate_labels(bundle["labels"])
    _validate_child_ids(bundle["statuses"], "statuses")
    sprints = _validate_child_ids(bundle["sprints"], "sprints")
    issues = _validate_child_ids(bundle["issues"], "issues")
    status_names = {
        _casefold_required(row, "name", f"statuses[{i}]")
        for i, row in enumerate(bundle["statuses"])
    }

    _require_user_ref(users, project["created_by"], "project.created_by")
    for i, status in enumerate(bundle["statuses"]):
        _require_fields(status, ("project_id", "name"), f"statuses[{i}]")
        _require_container_ref(
            status["project_id"], root_id, f"statuses[{i}].project_id"
        )
    for i, member in enumerate(bundle["members"]):
        _require_fields(member, ("project_id", "user_id", "added_by"), f"members[{i}]")
        _require_container_ref(
            member["project_id"], root_id, f"members[{i}].project_id"
        )
        _require_user_ref(users, member["user_id"], f"members[{i}].user_id")
        _require_user_ref(
            users, member["added_by"], f"members[{i}].added_by", nullable=True
        )
    for i, sprint in enumerate(bundle["sprints"]):
        _require_fields(sprint, ("id", "project_id"), f"sprints[{i}]")
        _require_container_ref(
            sprint["project_id"], root_id, f"sprints[{i}].project_id"
        )
    for i, issue in enumerate(bundle["issues"]):
        _require_fields(
            issue,
            (
                "id",
                "status",
                "created_by",
                "assignee_id",
                "project_id",
                "parent_id",
                "sprint_id",
            ),
            f"issues[{i}]",
        )
        _require_container_ref(
            issue["project_id"], root_id, f"issues[{i}].project_id"
        )
        if _casefold_required(issue, "status", f"issues[{i}]") not in status_names:
            raise ValueError(f"issues[{i}].status is not present in bundle statuses")
        _require_user_ref(users, issue["created_by"], f"issues[{i}].created_by")
        _require_user_ref(
            users, issue["assignee_id"], f"issues[{i}].assignee_id", nullable=True
        )
        _require_id_ref(
            issues, issue["parent_id"], f"issues[{i}].parent_id", nullable=True
        )
        _require_id_ref(
            sprints, issue["sprint_id"], f"issues[{i}].sprint_id", nullable=True
        )
    for i, comment in enumerate(bundle["comments"]):
        _require_fields(comment, ("issue_id", "author_id"), f"comments[{i}]")
        _require_id_ref(issues, comment["issue_id"], f"comments[{i}].issue_id")
        _require_user_ref(users, comment["author_id"], f"comments[{i}].author_id")
    for i, contributor in enumerate(bundle["contributors"]):
        _require_fields(
            contributor,
            ("issue_id", "user_id", "added_by"),
            f"contributors[{i}]",
        )
        _require_id_ref(
            issues, contributor["issue_id"], f"contributors[{i}].issue_id"
        )
        _require_user_ref(users, contributor["user_id"], f"contributors[{i}].user_id")
        _require_user_ref(
            users,
            contributor["added_by"],
            f"contributors[{i}].added_by",
            nullable=True,
        )
    for i, link in enumerate(bundle["issue_links"]):
        _require_fields(link, ("from_id", "to_id", "created_by"), f"issue_links[{i}]")
        if link["from_id"] not in issues and link["to_id"] not in issues:
            raise ValueError(
                f"issue_links[{i}] must reference at least one bundled issue"
            )
        _require_user_ref(users, link["created_by"], f"issue_links[{i}].created_by")
    for i, label_link in enumerate(bundle["label_links"]):
        _require_fields(label_link, ("issue_id", "label_id"), f"label_links[{i}]")
        _require_id_ref(issues, label_link["issue_id"], f"label_links[{i}].issue_id")
        _require_id_ref(labels, label_link["label_id"], f"label_links[{i}].label_id")
    _validate_attachment_refs(bundle["attachments"], "issue", issues, users)
    _validate_cross_links(bundle["cross_links"], {"issue": issues})
    _validate_activity_refs(
        bundle["activity"], {"project": {root_id}, "issue": issues}, users
    )


def _validate_space_bundle(bundle: dict) -> None:
    root_id = bundle["root_id"]
    space = _require_object(bundle, "space")
    _require_fields(space, ("id", "key", "created_by"), "space")
    if space["id"] != root_id:
        raise ValueError("bundle.space.id must match bundle.root_id")

    for key in (
        "members",
        "pages",
        "versions",
        "comments",
        "labels",
        "label_links",
        "attachments",
        "cross_links",
        "activity",
        "users",
    ):
        _require_list(bundle, key)

    users = _validate_users(bundle["users"])
    labels = _validate_labels(bundle["labels"])
    pages = _validate_child_ids(bundle["pages"], "pages")

    _require_user_ref(users, space["created_by"], "space.created_by")
    for i, member in enumerate(bundle["members"]):
        _require_fields(member, ("space_id", "user_id", "added_by"), f"members[{i}]")
        _require_container_ref(member["space_id"], root_id, f"members[{i}].space_id")
        _require_user_ref(users, member["user_id"], f"members[{i}].user_id")
        _require_user_ref(
            users, member["added_by"], f"members[{i}].added_by", nullable=True
        )
    for i, page in enumerate(bundle["pages"]):
        _require_fields(
            page,
            ("id", "space_id", "parent_id", "created_by", "updated_by"),
            f"pages[{i}]",
        )
        _require_container_ref(page["space_id"], root_id, f"pages[{i}].space_id")
        _require_id_ref(pages, page["parent_id"], f"pages[{i}].parent_id", nullable=True)
        _require_user_ref(users, page["created_by"], f"pages[{i}].created_by")
        _require_user_ref(
            users, page["updated_by"], f"pages[{i}].updated_by", nullable=True
        )
    for i, version in enumerate(bundle["versions"]):
        _require_fields(version, ("page_id", "edited_by"), f"versions[{i}]")
        _require_id_ref(pages, version["page_id"], f"versions[{i}].page_id")
        _require_user_ref(users, version["edited_by"], f"versions[{i}].edited_by")
    for i, comment in enumerate(bundle["comments"]):
        _require_fields(comment, ("page_id", "author_id"), f"comments[{i}]")
        _require_id_ref(pages, comment["page_id"], f"comments[{i}].page_id")
        _require_user_ref(users, comment["author_id"], f"comments[{i}].author_id")
    for i, label_link in enumerate(bundle["label_links"]):
        _require_fields(label_link, ("page_id", "label_id"), f"label_links[{i}]")
        _require_id_ref(pages, label_link["page_id"], f"label_links[{i}].page_id")
        _require_id_ref(labels, label_link["label_id"], f"label_links[{i}].label_id")
    _validate_attachment_refs(bundle["attachments"], "page", pages, users)
    _validate_cross_links(bundle["cross_links"], {"page": pages})
    _validate_activity_refs(
        bundle["activity"], {"space": {root_id}, "page": pages}, users
    )


def _base_import_report(bundle: dict) -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "kind": bundle["kind"],
        "root_id": bundle["root_id"],
        "ok": True,
        "status": "ready",
        "would_create": {},
        "would_reuse": {},
        "conflicts": [],
        "warnings": [],
    }


def _set_counts(
    report: dict,
    *,
    would_create: dict[str, int],
    would_reuse: dict[str, int],
) -> None:
    report["would_create"] = would_create
    report["would_reuse"] = would_reuse


def _finish_import_report(report: dict) -> dict:
    report["ok"] = not report["conflicts"]
    report["status"] = "ready" if report["ok"] else "blocked"
    return report


def _user_import_status(
    conn: sqlite3.Connection,
    users: list[dict],
    report: dict,
) -> dict[str, int]:
    existing = 0
    for user in users:
        found = _one(
            conn,
            "SELECT id, email, name, role, is_agent FROM users WHERE email = ?",
            (user["email"],),
        )
        if found is None:
            _add_conflict(
                report,
                "missing_user",
                f"target database has no user for {user['email']!r}",
                path=f"users[{user['email']}]",
                value=user["email"],
            )
            continue
        existing += 1
        if found["role"] != user["role"]:
            _add_warning(
                report,
                "user_role_differs",
                f"user {user['email']!r} has role {found['role']!r} in target "
                f"but {user['role']!r} in bundle",
                path=f"users[{user['email']}].role",
            )
        if found["is_agent"] != bool(user["is_agent"]):
            _add_warning(
                report,
                "user_agent_flag_differs",
                f"user {user['email']!r} has is_agent={found['is_agent']} in "
                f"target but {bool(user['is_agent'])} in bundle",
                path=f"users[{user['email']}].is_agent",
            )
    return {"existing": existing, "missing": len(users) - existing}


def _label_import_status(
    conn: sqlite3.Connection,
    labels: list[dict],
    report: dict,
) -> dict[str, int]:
    existing = 0
    for label in labels:
        found = _one(
            conn,
            "SELECT id, name, color FROM labels WHERE name = ?",
            (label["name"],),
        )
        if found is None:
            continue
        existing += 1
        if found["color"].lower() != label["color"].lower():
            _add_warning(
                report,
                "label_color_differs",
                f"label {label['name']!r} exists with color {found['color']!r}; "
                f"bundle color is {label['color']!r}",
                path=f"labels[{label['name']}].color",
                existing_id=found["id"],
            )
    return {"existing": existing, "missing": len(labels) - existing}


def _warn_external_issue_links(
    report: dict, issue_links: list[dict], issue_ids: set[int]
) -> None:
    count = sum(
        1
        for link in issue_links
        if link["from_id"] not in issue_ids or link["to_id"] not in issue_ids
    )
    if count:
        _add_warning(
            report,
            "external_issue_links_require_mapping",
            f"{count} issue link(s) reference issues outside this bundle",
        )


def _warn_external_cross_links(
    report: dict,
    cross_links: list[dict],
    internal: dict[str, set[int]],
) -> None:
    count = 0
    for link in cross_links:
        source_inside = link["source_id"] in internal.get(link["source_kind"], set())
        target_inside = link["target_id"] in internal.get(link["target_kind"], set())
        if not (source_inside and target_inside):
            count += 1
    if count:
        _add_warning(
            report,
            "external_cross_links_require_mapping",
            f"{count} cross-link(s) reference records outside this bundle",
        )


def _warn_attachment_manifests(report: dict, attachments: list[dict]) -> None:
    if attachments:
        _add_warning(
            report,
            "attachment_blobs_not_included",
            f"{len(attachments)} attachment manifest row(s) do not include raw blobs",
        )


def _validate_users(users: list[dict]) -> set[int]:
    ids: set[int] = set()
    emails: set[str] = set()
    for i, user in enumerate(users):
        _require_fields(
            user,
            ("id", "email", "name", "role", "is_agent"),
            f"users[{i}]",
        )
        user_id = _require_int(user["id"], f"users[{i}].id")
        if user_id in ids:
            raise ValueError(f"users contains duplicate id {user_id}")
        ids.add(user_id)
        if not isinstance(user["email"], str) or not user["email"]:
            raise ValueError(f"users[{i}].email must be a non-empty string")
        email_key = user["email"].casefold()
        if email_key in emails:
            raise ValueError(f"users contains duplicate email {user['email']!r}")
        emails.add(email_key)
    return ids


def _validate_labels(labels: list[dict]) -> set[int]:
    ids: set[int] = set()
    names: set[str] = set()
    for i, label in enumerate(labels):
        _require_fields(label, ("id", "name", "color"), f"labels[{i}]")
        label_id = _require_int(label["id"], f"labels[{i}].id")
        if label_id in ids:
            raise ValueError(f"labels contains duplicate id {label_id}")
        ids.add(label_id)
        name = _casefold_required(label, "name", f"labels[{i}]")
        if name in names:
            raise ValueError(f"labels contains duplicate name {label['name']!r}")
        names.add(name)
    return ids


def _validate_child_ids(rows: list[dict], path: str) -> set[int]:
    ids: set[int] = set()
    for i, row in enumerate(rows):
        _require_fields(row, ("id",), f"{path}[{i}]")
        row_id = _require_int(row["id"], f"{path}[{i}].id")
        if row_id in ids:
            raise ValueError(f"{path} contains duplicate id {row_id}")
        ids.add(row_id)
    return ids


def _validate_attachment_refs(
    attachments: list[dict],
    target_kind: str,
    target_ids: set[int],
    users: set[int],
) -> None:
    for i, attachment in enumerate(attachments):
        _require_fields(
            attachment,
            ("target_kind", "target_id", "filename", "uploaded_by"),
            f"attachments[{i}]",
        )
        if attachment["target_kind"] != target_kind:
            raise ValueError(f"attachments[{i}].target_kind must be {target_kind!r}")
        _require_id_ref(
            target_ids, attachment["target_id"], f"attachments[{i}].target_id"
        )
        _require_user_ref(
            users, attachment["uploaded_by"], f"attachments[{i}].uploaded_by"
        )


def _validate_cross_links(
    cross_links: list[dict], internal: dict[str, set[int]]
) -> None:
    for i, link in enumerate(cross_links):
        _require_fields(
            link,
            ("source_kind", "source_id", "target_kind", "target_id"),
            f"cross_links[{i}]",
        )
        source_inside = link["source_id"] in internal.get(link["source_kind"], set())
        target_inside = link["target_id"] in internal.get(link["target_kind"], set())
        if not source_inside and not target_inside:
            raise ValueError(
                f"cross_links[{i}] must reference at least one bundled record"
            )


def _validate_activity_refs(
    events: list[dict], targets: dict[str, set[int]], users: set[int]
) -> None:
    for i, event in enumerate(events):
        _require_fields(
            event,
            ("actor_id", "target_kind", "target_id"),
            f"activity[{i}]",
        )
        _require_user_ref(users, event["actor_id"], f"activity[{i}].actor_id")
        if event["target_id"] not in targets.get(event["target_kind"], set()):
            raise ValueError(
                f"activity[{i}] must target a bundled {event['target_kind']!r} record"
            )


def _require_object(bundle: dict, key: str) -> dict:
    value = bundle.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"bundle.{key} must be an object")
    return value


def _require_list(bundle: dict, key: str) -> list:
    value = bundle.get(key)
    if not isinstance(value, list):
        raise ValueError(f"bundle.{key} must be a list")
    return value


def _require_fields(row: dict, fields: Iterable[str], path: str) -> None:
    if not isinstance(row, dict):
        raise ValueError(f"{path} must be an object")
    for field in fields:
        if field not in row:
            raise ValueError(f"{path}.{field} is required")


def _require_int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{path} must be an integer")
    return value


def _require_container_ref(value: Any, expected: int, path: str) -> None:
    actual = _require_int(value, path)
    if actual != expected:
        raise ValueError(f"{path} must match bundle.root_id")


def _require_id_ref(
    ids: set[int], value: Any, path: str, *, nullable: bool = False
) -> None:
    if value is None and nullable:
        return
    actual = _require_int(value, path)
    if actual not in ids:
        raise ValueError(f"{path} references an id outside this bundle")


def _require_user_ref(
    user_ids: set[int], value: Any, path: str, *, nullable: bool = False
) -> None:
    if value is None and nullable:
        return
    actual = _require_int(value, path)
    if actual not in user_ids:
        raise ValueError(f"{path} references a user outside bundle.users")


def _casefold_required(row: dict, key: str, path: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}.{key} must be a non-empty string")
    return value.casefold()


def _ids_from(rows: list[dict], key: str, path: str) -> list[int]:
    return [_require_int(row[key], f"{path}[{i}].{key}") for i, row in enumerate(rows)]


def _add_conflict(
    report: dict,
    code: str,
    message: str,
    *,
    path: str | None = None,
    value: Any | None = None,
    existing_id: int | None = None,
) -> None:
    item = {"code": code, "message": message}
    if path is not None:
        item["path"] = path
    if value is not None:
        item["value"] = value
    if existing_id is not None:
        item["existing_id"] = existing_id
    report["conflicts"].append(item)


def _add_warning(
    report: dict,
    code: str,
    message: str,
    *,
    path: str | None = None,
    existing_id: int | None = None,
) -> None:
    item = {"code": code, "message": message}
    if path is not None:
        item["path"] = path
    if existing_id is not None:
        item["existing_id"] = existing_id
    report["warnings"].append(item)


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
