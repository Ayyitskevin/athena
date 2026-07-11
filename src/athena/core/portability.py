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
import secrets
import sqlite3
from typing import Any, Iterable

from athena.core import links

SCHEMA = "athena.portability.v1"
MANIFEST_SCHEMA = "athena.import_manifest.v1"
IMPORT_RESULT_SCHEMA = "athena.import_result.v1"
ATTACHMENT_POLICIES = ("skip", "require-blobs")

# Bounds on a bundle before we load/replay it. A bundle is imported by loading the whole
# JSON into memory and replaying one INSERT per row inside a single write transaction, so
# an oversized/hostile bundle could exhaust memory or hold the write lock. These caps are
# generous for a real migration but refuse a pathological input up front.
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024  # 64 MiB on disk
_MAX_BUNDLE_ROWS = 500_000  # total rows across every top-level array

# An imported activity row's verb/timestamp come from an untrusted bundle. Bound the
# free-text verb so a hostile bundle can't pack the audit log, and parse timestamps in
# the trail's stored format (datetime('now')) so a bundle can't back/forward-date events.
_MAX_IMPORTED_VERB_LEN = 80
_IMPORT_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

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

    _write_json_file(destination, bundle)
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


def write_import_manifest_database(
    db_path: str | Path,
    bundle_path: str | Path,
    manifest_path: str | Path,
    *,
    attachment_policy: str = "skip",
    overwrite: bool = False,
) -> dict:
    """Write an import replay manifest and return the manifest."""
    database = Path(db_path)
    bundle_file = Path(bundle_path)
    destination = Path(manifest_path)
    if not database.exists():
        raise FileNotFoundError(f"database path does not exist: {database}")
    if not database.is_file():
        raise FileNotFoundError(f"database path is not a file: {database}")
    if not bundle_file.exists():
        raise FileNotFoundError(f"bundle path does not exist: {bundle_file}")
    if not bundle_file.is_file():
        raise FileNotFoundError(f"bundle path is not a file: {bundle_file}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"manifest path already exists: {destination}")

    bundle = _load_bundle(bundle_file)
    conn = _connect_readonly(database)
    try:
        manifest = build_import_manifest(
            conn,
            bundle,
            attachment_policy=attachment_policy,
        )
    finally:
        conn.close()

    _write_json_file(destination, manifest)
    return manifest


def import_manifest_database(
    db_path: str | Path,
    bundle_path: str | Path,
    manifest_path: str | Path,
) -> dict:
    """Replay a ready import manifest into ``db_path`` and return the result."""
    database = Path(db_path)
    bundle_file = Path(bundle_path)
    manifest_file = Path(manifest_path)
    if not database.exists():
        raise FileNotFoundError(f"database path does not exist: {database}")
    if not database.is_file():
        raise FileNotFoundError(f"database path is not a file: {database}")
    if not bundle_file.exists():
        raise FileNotFoundError(f"bundle path does not exist: {bundle_file}")
    if not bundle_file.is_file():
        raise FileNotFoundError(f"bundle path is not a file: {bundle_file}")
    if not manifest_file.exists():
        raise FileNotFoundError(f"manifest path does not exist: {manifest_file}")
    if not manifest_file.is_file():
        raise FileNotFoundError(f"manifest path is not a file: {manifest_file}")

    bundle = _load_bundle(bundle_file)
    manifest = _load_manifest(manifest_file)
    conn = _connect_readwrite(database)
    try:
        return replay_import_manifest(conn, bundle, manifest)
    finally:
        conn.close()


def export_bundle(conn: sqlite3.Connection, kind: str, target_id: int) -> dict:
    """Return the in-memory export bundle for one project or space."""
    if kind == "project":
        return _project_bundle(conn, target_id)
    if kind == "space":
        return _space_bundle(conn, target_id)
    raise ValueError("export kind must be 'project' or 'space'")


def build_import_manifest(
    conn: sqlite3.Connection,
    bundle: dict,
    *,
    attachment_policy: str = "skip",
) -> dict:
    """Build a replay manifest for a future selective import."""
    if attachment_policy not in ATTACHMENT_POLICIES:
        raise ValueError(
            "attachment_policy must be one of: " + ", ".join(ATTACHMENT_POLICIES)
        )
    bundle = _validate_bundle(bundle)
    dry_run = dry_run_import_bundle(conn, bundle)
    policy = _attachment_policy(bundle["attachments"], attachment_policy)
    manifest_conflicts = []
    if policy["status"] == "blocked":
        manifest_conflicts.append(
            {
                "code": "attachment_blobs_required",
                "message": "attachment_policy=require-blobs blocks bundles with attachment manifests because V1 bundles do not include raw blobs",
            }
        )

    id_map = _import_id_map(conn, bundle)
    conflicts = [*dry_run["conflicts"], *manifest_conflicts]
    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": 1,
        "bundle_schema": SCHEMA,
        "bundle_schema_version": bundle["schema_version"],
        "kind": bundle["kind"],
        "root_id": bundle["root_id"],
        "generated_at": _utc_now(),
        "ok": not conflicts,
        "status": "ready" if not conflicts else "blocked",
        "attachment_policy": policy,
        "dry_run": dry_run,
        "conflicts": conflicts,
        "warnings": dry_run["warnings"],
        "id_map": id_map,
        "operations": _import_operations(bundle, policy, id_map),
    }


def replay_import_manifest(
    conn: sqlite3.Connection,
    bundle: dict,
    manifest: dict,
) -> dict:
    """Replay a ready import manifest into ``conn`` transactionally."""
    bundle = _validate_bundle(bundle)
    manifest = _validate_import_manifest(manifest, bundle)
    policy = manifest["attachment_policy"]["mode"]

    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = build_import_manifest(conn, bundle, attachment_policy=policy)
        if fresh["status"] != "ready":
            raise ValueError(
                "import manifest is no longer ready; regenerate it with "
                "athena-import-manifest"
            )
        if _manifest_signature(fresh) != _manifest_signature(manifest):
            raise ValueError(
                "import manifest is stale; regenerate it with athena-import-manifest"
            )

        if bundle["kind"] == "project":
            result = _replay_project_import(conn, bundle, manifest)
        elif bundle["kind"] == "space":
            result = _replay_space_import(conn, bundle, manifest)
        else:
            raise ValueError("bundle kind must be 'project' or 'space'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


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
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot read bundle: {exc}") from exc
    if size > _MAX_BUNDLE_BYTES:
        raise ValueError(
            f"bundle is too large ({size} bytes > {_MAX_BUNDLE_BYTES}); refusing to load"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"bundle is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("bundle must be a JSON object")
    return data


def _load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    return data


def _write_json_file(destination: Path, data: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _connect_readwrite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
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


def _replay_project_import(
    conn: sqlite3.Connection,
    bundle: dict,
    manifest: dict,
) -> dict:
    _validate_project_bundle(bundle)
    user_map, reused_users = _resolve_user_map(conn, bundle["users"])
    label_map, created_labels, reused_labels = _resolve_label_map(
        conn, bundle["labels"]
    )
    result = _base_import_result(bundle, manifest)

    project = bundle["project"]
    max_seq = max((row["project_seq"] or 0 for row in bundle["issues"]), default=0)
    issue_counter = max(project.get("issue_counter") or 0, max_seq)
    cur = conn.execute(
        "INSERT INTO projects "
        "(id, name, key, description, created_by, created_at, issue_counter, visibility, "
        "activity_scope_key) "
        "SELECT next_id, ?, ?, ?, ?, ?, ?, ?, ? "
        "FROM activity_target_id_sequences WHERE target_kind = 'project'",
        (
            project["name"],
            project["key"],
            project.get("description", ""),
            user_map[project["created_by"]],
            project["created_at"],
            issue_counter,
            project.get("visibility", "public"),
            secrets.token_hex(16),
        ),
    )
    project_id = cur.lastrowid

    for status in bundle["statuses"]:
        conn.execute(
            "INSERT INTO project_statuses "
            "(project_id, name, category, position) VALUES (?, ?, ?, ?)",
            (
                project_id,
                status["name"],
                status.get("category", "todo"),
                status.get("position", 0),
            ),
        )

    sprint_map: dict[int, int] = {}
    for sprint in bundle["sprints"]:
        cur = conn.execute(
            "INSERT INTO sprints "
            "(project_id, name, goal, state, start_date, end_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                sprint["name"],
                sprint.get("goal", ""),
                sprint.get("state", "planned"),
                sprint.get("start_date"),
                sprint.get("end_date"),
                sprint["created_at"],
            ),
        )
        sprint_map[sprint["id"]] = cur.lastrowid

    issue_map: dict[int, int] = {}
    for issue in bundle["issues"]:
        cur = conn.execute(
            "INSERT INTO issues "
            "(title, body, status, priority, created_by, created_at, assignee_id, "
            "project_id, project_seq, parent_id, sprint_id, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue["title"],
                issue.get("body", ""),
                issue["status"],
                issue.get("priority", "medium"),
                user_map[issue["created_by"]],
                issue["created_at"],
                _map_optional(user_map, issue.get("assignee_id")),
                project_id,
                issue.get("project_seq"),
                None,
                _map_optional(sprint_map, issue.get("sprint_id")),
                issue.get("archived_at"),
            ),
        )
        issue_map[issue["id"]] = cur.lastrowid

    for issue in bundle["issues"]:
        body = _rewrite_body_refs(issue.get("body", ""), {"issue": issue_map})
        conn.execute(
            "UPDATE issues SET body = ?, parent_id = ? WHERE id = ?",
            (
                body,
                _map_optional(issue_map, issue.get("parent_id")),
                issue_map[issue["id"]],
            ),
        )

    member_count = 0
    for member in bundle["members"]:
        cur = conn.execute(
            "INSERT OR IGNORE INTO project_members "
            "(project_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (
                project_id,
                user_map[member["user_id"]],
                _map_optional(user_map, member.get("added_by")),
                member["added_at"],
            ),
        )
        member_count += max(cur.rowcount, 0)

    comment_count = 0
    for comment in bundle["comments"]:
        conn.execute(
            "INSERT INTO comments (issue_id, author_id, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                issue_map[comment["issue_id"]],
                user_map[comment["author_id"]],
                comment["body"],
                comment["created_at"],
            ),
        )
        comment_count += 1

    contributor_count = 0
    for contributor in bundle["contributors"]:
        cur = conn.execute(
            "INSERT OR IGNORE INTO issue_contributors "
            "(issue_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (
                issue_map[contributor["issue_id"]],
                user_map[contributor["user_id"]],
                _map_optional(user_map, contributor.get("added_by")),
                contributor["added_at"],
            ),
        )
        contributor_count += max(cur.rowcount, 0)

    issue_link_count, skipped_issue_links = _insert_project_issue_links(
        conn, bundle["issue_links"], issue_map, user_map
    )

    issue_label_count = 0
    for label_link in bundle["label_links"]:
        cur = conn.execute(
            "INSERT OR IGNORE INTO issue_labels (issue_id, label_id) VALUES (?, ?)",
            (issue_map[label_link["issue_id"]], label_map[label_link["label_id"]]),
        )
        issue_label_count += max(cur.rowcount, 0)

    link_count, skipped_cross_links = _insert_internal_cross_links(
        conn, bundle["cross_links"], {"issue": issue_map}
    )
    activity_count, activity_map = _insert_activity(
        conn,
        bundle["activity"],
        user_map,
        {"project": {project["id"]: project_id}, "issue": issue_map},
    )
    search_count = _index_imported_issues(conn, bundle["issues"], issue_map)

    result["created"] = {
        "projects": 1,
        "project_statuses": len(bundle["statuses"]),
        "project_members": member_count,
        "sprints": len(sprint_map),
        "issues": len(issue_map),
        "comments": comment_count,
        "issue_contributors": contributor_count,
        "issue_links": issue_link_count,
        "labels": created_labels,
        "issue_labels": issue_label_count,
        "links": link_count,
        "activity": activity_count,
        "search_index": search_count,
    }
    result["reused"] = {"users": reused_users, "labels": reused_labels}
    result["skipped"] = {
        "attachments": len(bundle["attachments"]),
        "external_issue_links": skipped_issue_links,
        "external_cross_links": skipped_cross_links,
    }
    result["id_map"] = {
        "users": _map_rows(bundle["users"], user_map),
        "labels": _map_rows(bundle["labels"], label_map),
        "project": {"source_id": project["id"], "target_id": project_id},
        "sprints": _map_rows(bundle["sprints"], sprint_map),
        "issues": _map_rows(bundle["issues"], issue_map),
        "activity": _map_rows(bundle["activity"], activity_map),
    }
    return result


def _replay_space_import(
    conn: sqlite3.Connection,
    bundle: dict,
    manifest: dict,
) -> dict:
    _validate_space_bundle(bundle)
    user_map, reused_users = _resolve_user_map(conn, bundle["users"])
    label_map, created_labels, reused_labels = _resolve_label_map(
        conn, bundle["labels"]
    )
    result = _base_import_result(bundle, manifest)

    space = bundle["space"]
    cur = conn.execute(
        "INSERT INTO spaces "
        "(id, key, name, description, created_by, created_at, visibility) "
        "SELECT next_id, ?, ?, ?, ?, ?, ? "
        "FROM activity_target_id_sequences WHERE target_kind = 'space'",
        (
            space["key"],
            space["name"],
            space.get("description", ""),
            user_map[space["created_by"]],
            space["created_at"],
            space.get("visibility", "public"),
        ),
    )
    space_id = cur.lastrowid

    page_map: dict[int, int] = {}
    for page in bundle["pages"]:
        cur = conn.execute(
            "INSERT INTO pages "
            "(id, space_id, parent_id, title, body, created_by, created_at, updated_by, "
            "updated_at) SELECT next_id, ?, ?, ?, ?, ?, ?, ?, ? "
            "FROM activity_target_id_sequences WHERE target_kind = 'page'",
            (
                space_id,
                None,
                page["title"],
                page.get("body", ""),
                user_map[page["created_by"]],
                page["created_at"],
                _map_optional(user_map, page.get("updated_by")),
                page.get("updated_at"),
            ),
        )
        page_map[page["id"]] = cur.lastrowid

    for page in bundle["pages"]:
        body = _rewrite_body_refs(page.get("body", ""), {"page": page_map})
        conn.execute(
            "UPDATE pages SET body = ?, parent_id = ? WHERE id = ?",
            (
                body,
                _map_optional(page_map, page.get("parent_id")),
                page_map[page["id"]],
            ),
        )

    member_count = 0
    for member in bundle["members"]:
        cur = conn.execute(
            "INSERT OR IGNORE INTO space_members "
            "(space_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (
                space_id,
                user_map[member["user_id"]],
                _map_optional(user_map, member.get("added_by")),
                member["added_at"],
            ),
        )
        member_count += max(cur.rowcount, 0)

    version_count = 0
    for version in bundle["versions"]:
        conn.execute(
            "INSERT INTO page_versions "
            "(page_id, version, title, body, edited_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                page_map[version["page_id"]],
                version["version"],
                version["title"],
                _rewrite_body_refs(version.get("body", ""), {"page": page_map}),
                user_map[version["edited_by"]],
                version["created_at"],
            ),
        )
        version_count += 1

    comment_count = 0
    for comment in bundle["comments"]:
        conn.execute(
            "INSERT INTO page_comments (page_id, author_id, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                page_map[comment["page_id"]],
                user_map[comment["author_id"]],
                comment["body"],
                comment["created_at"],
            ),
        )
        comment_count += 1

    page_label_count = 0
    for label_link in bundle["label_links"]:
        cur = conn.execute(
            "INSERT OR IGNORE INTO page_labels (page_id, label_id) VALUES (?, ?)",
            (page_map[label_link["page_id"]], label_map[label_link["label_id"]]),
        )
        page_label_count += max(cur.rowcount, 0)

    link_count, skipped_cross_links = _insert_internal_cross_links(
        conn, bundle["cross_links"], {"page": page_map}
    )
    activity_count, activity_map = _insert_activity(
        conn,
        bundle["activity"],
        user_map,
        {"space": {space["id"]: space_id}, "page": page_map},
    )
    search_count = _index_imported_pages(conn, bundle["pages"], page_map)

    result["created"] = {
        "spaces": 1,
        "space_members": member_count,
        "pages": len(page_map),
        "page_versions": version_count,
        "page_comments": comment_count,
        "labels": created_labels,
        "page_labels": page_label_count,
        "links": link_count,
        "activity": activity_count,
        "search_index": search_count,
    }
    result["reused"] = {"users": reused_users, "labels": reused_labels}
    result["skipped"] = {
        "attachments": len(bundle["attachments"]),
        "external_cross_links": skipped_cross_links,
    }
    result["id_map"] = {
        "users": _map_rows(bundle["users"], user_map),
        "labels": _map_rows(bundle["labels"], label_map),
        "space": {"source_id": space["id"], "target_id": space_id},
        "pages": _map_rows(bundle["pages"], page_map),
        "activity": _map_rows(bundle["activity"], activity_map),
    }
    return result


def _base_import_result(bundle: dict, manifest: dict) -> dict:
    return {
        "schema": IMPORT_RESULT_SCHEMA,
        "schema_version": 1,
        "kind": bundle["kind"],
        "root_id": bundle["root_id"],
        "imported_at": _utc_now(),
        "ok": True,
        "status": "imported",
        "created": {},
        "reused": {},
        "skipped": {},
        "warnings": manifest["warnings"],
        "id_map": {},
    }


def _resolve_user_map(
    conn: sqlite3.Connection, users: list[dict]
) -> tuple[dict[int, int], int]:
    user_map: dict[int, int] = {}
    for user in users:
        found = _one(conn, "SELECT id FROM users WHERE email = ?", (user["email"],))
        if found is None:
            raise ValueError(
                "import manifest is no longer ready; missing target user "
                f"{user['email']!r}"
            )
        user_map[user["id"]] = found["id"]
    return user_map, len(user_map)


def _resolve_label_map(
    conn: sqlite3.Connection, labels: list[dict]
) -> tuple[dict[int, int], int, int]:
    label_map: dict[int, int] = {}
    created = 0
    reused = 0
    for label in labels:
        found = _one(conn, "SELECT id FROM labels WHERE name = ?", (label["name"],))
        if found is not None:
            label_map[label["id"]] = found["id"]
            reused += 1
            continue
        cur = conn.execute(
            "INSERT INTO labels (name, color) VALUES (?, ?)",
            (label["name"], label["color"]),
        )
        label_map[label["id"]] = cur.lastrowid
        created += 1
    return label_map, created, reused


def _insert_project_issue_links(
    conn: sqlite3.Connection,
    issue_links: list[dict],
    issue_map: dict[int, int],
    user_map: dict[int, int],
) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    for link in issue_links:
        if link["from_id"] not in issue_map or link["to_id"] not in issue_map:
            skipped += 1
            continue
        from_id = issue_map[link["from_id"]]
        to_id = issue_map[link["to_id"]]
        if from_id == to_id:
            skipped += 1
            continue
        if link["kind"] == "relates" and from_id > to_id:
            from_id, to_id = to_id, from_id
        cur = conn.execute(
            "INSERT OR IGNORE INTO issue_links "
            "(from_id, to_id, kind, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                from_id,
                to_id,
                link["kind"],
                user_map[link["created_by"]],
                link["created_at"],
            ),
        )
        inserted += max(cur.rowcount, 0)
    return inserted, skipped


def _insert_internal_cross_links(
    conn: sqlite3.Connection,
    cross_links: list[dict],
    maps: dict[str, dict[int, int]],
) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    for link in cross_links:
        source_map = maps.get(link["source_kind"])
        target_map = maps.get(link["target_kind"])
        if (
            source_map is None
            or target_map is None
            or link["source_id"] not in source_map
            or link["target_id"] not in target_map
        ):
            skipped += 1
            continue
        source_id = source_map[link["source_id"]]
        target_id = target_map[link["target_id"]]
        if link["source_kind"] == link["target_kind"] and source_id == target_id:
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO links "
            "(source_kind, source_id, target_kind, target_id) VALUES (?, ?, ?, ?)",
            (link["source_kind"], source_id, link["target_kind"], target_id),
        )
        inserted += max(cur.rowcount, 0)
    return inserted, skipped


def _insert_activity(
    conn: sqlite3.Connection,
    events: list[dict],
    user_map: dict[int, int],
    target_maps: dict[str, dict[int, int]],
) -> tuple[int, dict[int, int]]:
    # Imported activity is foreign history replayed from an untrusted bundle. We keep it
    # as data — who/what/when travelled with the project — but must never let it forge
    # NATIVE provenance:
    #   - stamp imported_at (one instant per import) so every imported row is
    #     distinguishable from an app-recorded one and the whole import is auditable;
    #   - write run_id/parent_run_id/forked_from_event_id as NULL so a bundle can't tag its
    #     events with a run id that collides with a native run and thereby splice forged
    #     events into that run's replay / lineage / reconstruction;
    #   - clamp created_at to the import instant when the bundle's value is unparseable or
    #     in the future, so a bundle can't back- or forward-date the trail;
    #   - bound the free-text verb.
    imported_at = conn.execute("SELECT datetime('now')").fetchone()[0]
    ceiling = datetime.strptime(imported_at, _IMPORT_TS_FORMAT)
    activity_map: dict[int, int] = {}
    for event in events:
        target_map = target_maps.get(event["target_kind"])
        if target_map is None or event["target_id"] not in target_map:
            continue
        cur = conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, created_at, "
            "run_id, parent_run_id, forked_from_event_id, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_map[event["actor_id"]],
                _bounded_verb(event.get("verb")),
                event["target_kind"],
                target_map[event["target_id"]],
                event.get("detail", ""),
                _bounded_created_at(event.get("created_at"), imported_at, ceiling),
                None,
                None,
                None,
                imported_at,
            ),
        )
        activity_map[event["id"]] = cur.lastrowid
    return len(activity_map), activity_map


def _bounded_verb(raw: object) -> str:
    # A verb from an untrusted bundle: coerce to a bounded, printable string so it can't
    # pack or corrupt the trail. Native verbs are short tokens set only by activity.py, so
    # a well-formed export passes through unchanged; only garbage is neutralized.
    text = raw if isinstance(raw, str) else ""
    text = "".join(ch for ch in text if ch.isprintable()).strip()[:_MAX_IMPORTED_VERB_LEN]
    return text or "imported"


def _bounded_created_at(raw: object, fallback: str, ceiling: datetime) -> str:
    # Keep the bundle's own timestamp (honest migrated history) only when it parses in the
    # trail's stored format and is not after the import instant; otherwise fall back to the
    # import instant. Blocks a bundle from back/forward-dating the audit trail.
    if not isinstance(raw, str):
        return fallback
    try:
        parsed = datetime.strptime(raw, _IMPORT_TS_FORMAT)
    except ValueError:
        return fallback
    return fallback if parsed > ceiling else raw


def _index_imported_issues(
    conn: sqlite3.Connection, issues: list[dict], issue_map: dict[int, int]
) -> int:
    count = 0
    for issue in issues:
        source_id = issue["id"]
        target_id = issue_map[source_id]
        body = _rewrite_body_refs(issue.get("body", ""), {"issue": issue_map})
        _replace_search_index(
            conn, kind="issue", source_id=target_id, title=issue["title"], body=body
        )
        count += 1
    return count


def _index_imported_pages(
    conn: sqlite3.Connection, pages: list[dict], page_map: dict[int, int]
) -> int:
    count = 0
    for page in pages:
        source_id = page["id"]
        target_id = page_map[source_id]
        body = _rewrite_body_refs(page.get("body", ""), {"page": page_map})
        _replace_search_index(
            conn, kind="page", source_id=target_id, title=page["title"], body=body
        )
        count += 1
    return count


def _replace_search_index(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_id: int,
    title: str,
    body: str,
) -> None:
    conn.execute(
        "DELETE FROM search_index WHERE kind = ? AND source_id = ?",
        (kind, source_id),
    )
    conn.execute(
        "INSERT INTO search_index (kind, source_id, title, body) VALUES (?, ?, ?, ?)",
        (kind, source_id, title, body),
    )


def _rewrite_body_refs(body: str | None, maps: dict[str, dict[int, int]]) -> str:
    def replace(match) -> str:
        kind = match.group(1)
        source_id = int(match.group(2))
        target_map = maps.get(kind)
        if target_map is None or source_id not in target_map:
            return match.group(0)
        return f"[[{kind}:{target_map[source_id]}]]"

    return links.REF_RE.sub(replace, body or "")


def _map_optional(mapping: dict[int, int], source_id: int | None) -> int | None:
    if source_id is None:
        return None
    return mapping[source_id]


def _map_rows(rows: list[dict], mapping: dict[int, int]) -> list[dict]:
    return [
        {"source_id": row["id"], "target_id": mapping[row["id"]]}
        for row in rows
        if row["id"] in mapping
    ]


def _import_id_map(conn: sqlite3.Connection, bundle: dict) -> dict:
    mapped = {
        "users": _user_id_map(conn, bundle["users"]),
        "labels": _label_id_map(conn, bundle["labels"]),
        "attachments": _attachment_id_map(bundle["attachments"]),
    }
    if bundle["kind"] == "project":
        mapped |= {
            "project": _create_id_ref("project", bundle["project"]["id"]),
            "project_statuses": _create_id_refs(
                "project_status", bundle["statuses"]
            ),
            "sprints": _create_id_refs("sprint", bundle["sprints"]),
            "issues": _create_id_refs("issue", bundle["issues"]),
            "comments": _create_id_refs("comment", bundle["comments"]),
            "activity": _create_id_refs("activity", bundle["activity"]),
        }
    else:
        mapped |= {
            "space": _create_id_ref("space", bundle["space"]["id"]),
            "pages": _create_id_refs("page", bundle["pages"]),
            "page_versions": _create_id_refs("page_version", bundle["versions"]),
            "page_comments": _create_id_refs("page_comment", bundle["comments"]),
            "activity": _create_id_refs("activity", bundle["activity"]),
        }
    return mapped


def _user_id_map(conn: sqlite3.Connection, users: list[dict]) -> list[dict]:
    rows = []
    for user in users:
        found = _one(
            conn,
            "SELECT id, email FROM users WHERE email = ?",
            (user["email"],),
        )
        rows.append(
            {
                "source_id": user["id"],
                "source_email": user["email"],
                "action": "reuse" if found else "missing",
                "target_id": found["id"] if found else None,
            }
        )
    return rows


def _label_id_map(conn: sqlite3.Connection, labels: list[dict]) -> list[dict]:
    rows = []
    for label in labels:
        found = _one(
            conn,
            "SELECT id, name FROM labels WHERE name = ?",
            (label["name"],),
        )
        row = {
            "source_id": label["id"],
            "name": label["name"],
            "color": label["color"],
            "action": "reuse" if found else "create",
            "target_id": found["id"] if found else None,
        }
        if found is None:
            row["target_ref"] = _target_ref("label", label["id"])
        rows.append(row)
    return rows


def _attachment_id_map(attachments: list[dict]) -> list[dict]:
    return [
        {
            "source_id": attachment["id"],
            "source_target_kind": attachment["target_kind"],
            "source_target_id": attachment["target_id"],
            "filename": attachment["filename"],
            "action": "policy-controlled",
            "target_ref": _target_ref("attachment", attachment["id"]),
        }
        for attachment in attachments
    ]


def _create_id_refs(kind: str, rows: list[dict]) -> list[dict]:
    return [_create_id_ref(kind, row["id"]) for row in rows]


def _create_id_ref(kind: str, source_id: int) -> dict:
    return {
        "source_id": source_id,
        "action": "create",
        "target_ref": _target_ref(kind, source_id),
    }


def _target_ref(kind: str, source_id: int) -> str:
    return f"{kind}:{source_id}"


def _attachment_policy(attachments: list[dict], policy: str) -> dict:
    if policy == "require-blobs" and attachments:
        return {
            "mode": policy,
            "status": "blocked",
            "attachment_count": len(attachments),
            "replay_action": "require_external_blobs",
        }
    return {
        "mode": policy,
        "status": "ready",
        "attachment_count": len(attachments),
        "replay_action": "skip" if policy == "skip" else "none",
    }


def _import_operations(bundle: dict, policy: dict, id_map: dict) -> list[dict]:
    if bundle["kind"] == "project":
        operations = _project_import_operations(bundle, id_map)
    else:
        operations = _space_import_operations(bundle, id_map)
    operations.append(
        {
            "op": "apply_attachment_policy",
            "policy": policy["mode"],
            "action": policy["replay_action"],
            "count": policy["attachment_count"],
        }
    )
    return operations


def _project_import_operations(bundle: dict, id_map: dict) -> list[dict]:
    return [
        {
            "op": "map_users",
            "reuse": _count_actions(id_map["users"], "reuse"),
            "missing": _count_actions(id_map["users"], "missing"),
        },
        {
            "op": "map_labels",
            "reuse": _count_actions(id_map["labels"], "reuse"),
            "create": _count_actions(id_map["labels"], "create"),
        },
        {
            "op": "create_labels",
            "count": _count_actions(id_map["labels"], "create"),
        },
        {
            "op": "create_project",
            "source_id": bundle["project"]["id"],
            "target_ref": id_map["project"]["target_ref"],
        },
        {"op": "create_project_statuses", "count": len(bundle["statuses"])},
        {"op": "create_sprints", "count": len(bundle["sprints"])},
        {"op": "create_issues", "count": len(bundle["issues"])},
        {"op": "create_project_members", "count": len(bundle["members"])},
        {"op": "create_comments", "count": len(bundle["comments"])},
        {
            "op": "create_issue_contributors",
            "count": len(bundle["contributors"]),
        },
        {"op": "create_issue_links", "count": len(bundle["issue_links"])},
        {"op": "create_issue_labels", "count": len(bundle["label_links"])},
        {"op": "create_cross_links", "count": len(bundle["cross_links"])},
        {"op": "create_activity_events", "count": len(bundle["activity"])},
    ]


def _space_import_operations(bundle: dict, id_map: dict) -> list[dict]:
    return [
        {
            "op": "map_users",
            "reuse": _count_actions(id_map["users"], "reuse"),
            "missing": _count_actions(id_map["users"], "missing"),
        },
        {
            "op": "map_labels",
            "reuse": _count_actions(id_map["labels"], "reuse"),
            "create": _count_actions(id_map["labels"], "create"),
        },
        {
            "op": "create_labels",
            "count": _count_actions(id_map["labels"], "create"),
        },
        {
            "op": "create_space",
            "source_id": bundle["space"]["id"],
            "target_ref": id_map["space"]["target_ref"],
        },
        {"op": "create_pages", "count": len(bundle["pages"])},
        {"op": "create_page_versions", "count": len(bundle["versions"])},
        {"op": "create_space_members", "count": len(bundle["members"])},
        {"op": "create_page_comments", "count": len(bundle["comments"])},
        {"op": "create_page_labels", "count": len(bundle["label_links"])},
        {"op": "create_cross_links", "count": len(bundle["cross_links"])},
        {"op": "create_activity_events", "count": len(bundle["activity"])},
    ]


def _count_actions(rows: list[dict], action: str) -> int:
    return sum(1 for row in rows if row["action"] == action)


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
    # Refuse a bundle with an unreasonable total number of rows before replay materializes
    # them one INSERT at a time in a single write transaction. Covers every import path
    # (the file-size cap in _load_bundle only guards the CLI; a bundle handed in-process
    # skips it).
    total_rows = sum(len(v) for v in bundle.values() if isinstance(v, list))
    if total_rows > _MAX_BUNDLE_ROWS:
        raise ValueError(
            f"bundle has too many rows ({total_rows} > {_MAX_BUNDLE_ROWS}); "
            "refusing to import"
        )
    return bundle


def _validate_import_manifest(manifest: dict, bundle: dict) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(
            "unsupported manifest schema: "
            f"{manifest.get('schema')!r}; expected {MANIFEST_SCHEMA!r}"
        )
    if manifest.get("schema_version") != 1:
        raise ValueError(
            "unsupported manifest schema_version: "
            f"{manifest.get('schema_version')!r}"
        )
    if manifest.get("bundle_schema") != SCHEMA:
        raise ValueError(
            "manifest bundle_schema does not match this importer: "
            f"{manifest.get('bundle_schema')!r}"
        )
    if manifest.get("bundle_schema_version") != bundle["schema_version"]:
        raise ValueError("manifest bundle_schema_version does not match bundle")
    if manifest.get("kind") != bundle["kind"]:
        raise ValueError("manifest kind does not match bundle")
    if manifest.get("root_id") != bundle["root_id"]:
        raise ValueError("manifest root_id does not match bundle")
    if manifest.get("ok") is not True or manifest.get("status") != "ready":
        raise ValueError("import manifest is not ready")

    policy = manifest.get("attachment_policy")
    if not isinstance(policy, dict):
        raise ValueError("manifest.attachment_policy must be an object")
    if policy.get("mode") not in ATTACHMENT_POLICIES:
        raise ValueError("manifest.attachment_policy.mode is not supported")
    if policy.get("status") != "ready":
        raise ValueError("manifest attachment policy is not ready")
    if not isinstance(manifest.get("warnings"), list):
        raise ValueError("manifest.warnings must be a list")
    return manifest


def _manifest_signature(manifest: dict) -> dict:
    keys = (
        "schema",
        "schema_version",
        "bundle_schema",
        "bundle_schema_version",
        "kind",
        "root_id",
        "attachment_policy",
        "dry_run",
        "conflicts",
        "warnings",
        "id_map",
        "operations",
    )
    return {key: manifest.get(key) for key in keys}


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
