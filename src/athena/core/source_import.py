"""Map common source exports into Athena portability bundles.

These adapters do not write Athena databases. They turn small Jira/Confluence JSON
exports into the same bundle shape that athena-import-dry-run, athena-import-
manifest, and athena-import already understand.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
import re
from pathlib import Path
from typing import Any

from athena.core import links, portability

SOURCE_KINDS = ("jira-project", "confluence-space")
SOURCE_MAPPING_REPORT_SCHEMA = "athena.source_mapping_report.v1"

_DEFAULT_LABEL_COLOR = "#6b7280"
_IMPORT_USER_EMAIL = "importer@import.local"
_IMPORT_USER_NAME = "Importer"
_ISSUE_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{0,9})-(\d+)\b")
_ATHENA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}$")
_PAGE_TOKEN_RE = re.compile(r"\[\[page:([A-Za-z0-9_.:-]+)\]\]")
_RESOURCE_ID_RE = re.compile(r'data-linked-resource-id=["\']([^"\']+)["\']')
_RI_CONTENT_ID_RE = re.compile(r'ri:content-id=["\']([^"\']+)["\']')
_RI_CONTENT_TITLE_RE = re.compile(r'ri:content-title=["\']([^"\']+)["\']')
_JIRA_ISSUE_KEYS = {"id", "key", "self", "fields"}
_JIRA_FIELD_KEYS = {
    "assignee",
    "attachment",  # recorded in attachment_manifest, not blob-imported
    "comment",
    "components",  # mapped into labels (name only)
    "created",
    "description",
    "fixVersions",  # mapped into version: labels (name only)
    "issuelinks",
    "issuetype",  # mapped into a type: label
    "labels",
    "parent",
    "priority",
    "project",
    "reporter",
    "status",
    "summary",
    "subtasks",  # used to backfill parent_id when child omits parent
    "versions",  # mapped into version: labels (name only)
}
_CONFLUENCE_PAGE_KEYS = {
    "ancestors",
    "body",
    "comments",
    "contentId",
    "createdBy",
    "history",
    "id",
    "key",
    "labels",
    "metadata",
    "parent_id",
    "parentId",
    "space",
    "title",
    "type",
    "version",
    "versions",
}


def write_source_bundle(
    source: str,
    source_path: str | Path,
    bundle_path: str | Path,
    *,
    project_key: str | None = None,
    project_name: str | None = None,
    space_key: str | None = None,
    space_name: str | None = None,
    report_path: str | Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Map ``source_path`` into an Athena portability bundle at ``bundle_path``."""
    source_file = Path(source_path)
    destination = Path(bundle_path)
    if not source_file.exists():
        raise FileNotFoundError(f"source path does not exist: {source_file}")
    if not source_file.is_file():
        raise FileNotFoundError(f"source path is not a file: {source_file}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"bundle path already exists: {destination}")
    report_destination = Path(report_path) if report_path is not None else None
    if report_destination is not None:
        if report_destination == destination:
            raise ValueError("report path must be different from bundle path")
        if report_destination.exists() and not overwrite:
            raise FileExistsError(f"report path already exists: {report_destination}")

    payload = _load_json(source_file)
    if source == "jira-project":
        bundle = map_jira_project(
            payload,
            project_key=project_key,
            project_name=project_name,
        )
    elif source == "confluence-space":
        bundle = map_confluence_space(
            payload,
            space_key=space_key,
            space_name=space_name,
        )
    else:
        raise ValueError("source must be one of: " + ", ".join(SOURCE_KINDS))

    _write_json_file(destination, bundle)
    if report_destination is not None:
        _write_json_file(report_destination, source_mapping_report(bundle))
    return destination


def source_mapping_report(bundle: dict) -> dict:
    """Return the source mapping report embedded in a mapped bundle."""
    source = bundle.get("source") if isinstance(bundle, dict) else {}
    report = source.get("mapping_report") if isinstance(source, dict) else None
    if not isinstance(report, dict):
        raise ValueError("bundle does not contain a source mapping report")
    return report


def map_jira_project(
    payload: Any,
    *,
    project_key: str | None = None,
    project_name: str | None = None,
) -> dict:
    """Map a Jira issue search/export JSON object into a project bundle."""
    issues_in = _extract_rows(
        payload,
        "issues",
        "values",
        nested_paths=(("data", "issues"), ("data", "values")),
    )
    if not issues_in:
        raise ValueError("jira-project payload contains no issues")

    now = _utc_now()
    report = _MappingReport("jira-project", now)
    users = _UserTable(now)
    # Prefer the first real issue object so a leading null/string does not crash
    # project-info derivation before per-row guards run.
    first_issue = next((row for row in issues_in if isinstance(row, dict)), None)
    if first_issue is None:
        raise ValueError("jira-project payload contains no issue objects")
    project = _jira_project_info(first_issue, project_key, project_name)
    project_id = 1
    owner_id = users.id_for(_jira_user(_field(first_issue, "reporter")))

    # This mapper emits ONE project bundle. A Jira search/board/filter export can span
    # multiple projects; a foreign-project issue would be force-lumped into this one and
    # its raw key number reused as project_seq, colliding with a same-numbered issue here
    # (WEB-3 and API-3 both -> seq 3, producing two issues keyed WEB-3). Keep only issues
    # in the derived project (by key prefix; keyless issues synthesize into it) and report
    # the rest as skipped.
    prefix_cf = project["key"].casefold()
    scoped_issues: list = []
    skipped_foreign = 0
    for i, issue in enumerate(issues_in, start=1):
        if not isinstance(issue, dict):
            report.note_unmapped("issues[]", "non-object issue row was skipped")
            continue
        candidate_key = _jira_issue_key(issue, project["key"], i)
        if (_jira_key_prefix(candidate_key) or project["key"]).casefold() != prefix_cf:
            skipped_foreign += 1
            report.note_unmapped(
                "issues[].fields.project",
                "issue belongs to a different project and was skipped",
            )
            continue
        scoped_issues.append(issue)
    issues_in = scoped_issues
    report.set_count("source_issues", len(issues_in) + skipped_foreign)
    report.set_count("skipped_foreign_project", skipped_foreign)
    if not issues_in:
        raise ValueError(
            "jira-project payload had issues, but none matched the target project "
            f"key {project['key']!r} after multi-project filtering"
        )

    issue_id_by_key: dict[str, int] = {}
    issue_seq_by_key: dict[str, int] = {}
    for i, issue in enumerate(issues_in, start=1):
        key = _jira_issue_key(issue, project["key"], i)
        issue_id_by_key[key] = i
        issue_seq_by_key[key] = _jira_issue_seq(key, i)

    status_ids: dict[str, int] = {}
    canonical_status: dict[str, str] = {}
    statuses: list[dict] = []
    labels_by_name: dict[str, dict] = {}
    label_links: list[dict] = []
    comments: list[dict] = []
    issue_links: list[dict] = []
    cross_links: list[dict] = []
    issue_rows: list[dict] = []
    comment_id = 1
    link_seen: set[tuple[int, int, str]] = set()

    for issue in issues_in:
        _report_jira_unmapped_fields(report, issue)
        key = _jira_issue_key(issue, project["key"], len(issue_rows) + 1)
        issue_id = issue_id_by_key[key]
        fields = issue.get("fields") if isinstance(issue, dict) else {}
        fields = fields if isinstance(fields, dict) else {}

        status_name = _jira_status_name(fields.get("status"))
        status_cf = status_name.casefold()
        if status_cf not in status_ids:
            status_ids[status_cf] = len(statuses) + 1
            canonical_status[status_cf] = status_name
            statuses.append(
                {
                    "id": len(statuses) + 1,
                    "project_id": project_id,
                    "name": status_name,
                    "category": _jira_status_category(fields.get("status")),
                    "position": len(statuses),
                }
            )

        reporter = _jira_user(fields.get("reporter"))
        assignee = _jira_user(fields.get("assignee"))
        created_by = users.id_for(reporter)
        assignee_id = users.id_for(assignee) if assignee is not None else None
        body = _rewrite_jira_issue_keys(
            _body_to_text(fields.get("description")),
            issue_id_by_key,
        )
        parent_key = _jira_parent_key(fields.get("parent"))
        if parent_key and parent_key not in issue_id_by_key:
            report.note_unmapped(
                "issues[].fields.parent",
                "parent issue is outside this source export",
            )

        issue_rows.append(
            {
                "id": issue_id,
                "title": str(fields.get("summary") or key),
                "body": body,
                # The canonical (first-seen) status-row name, not this issue's raw casing:
                # the project_statuses row keeps one casing under UNIQUE(name COLLATE
                # NOCASE), and Athena's category_of/is_done compare case-SENSITIVELY, so a
                # second-casing issue must store the same name to resolve its category.
                "status": canonical_status[status_cf],
                "priority": _jira_priority(fields.get("priority")),
                "created_by": created_by,
                "created_at": _string_or_now(fields.get("created"), now),
                "assignee_id": assignee_id,
                "project_id": project_id,
                "project_seq": issue_seq_by_key[key],
                "parent_id": issue_id_by_key.get(parent_key) if parent_key else None,
                "sprint_id": None,
                "archived_at": None,
            }
        )

        for ref_kind, ref_id in links.extract_refs(body):
            if ref_kind == "issue" and ref_id != issue_id:
                cross_links.append(
                    {
                        "source_kind": "issue",
                        "source_id": issue_id,
                        "target_kind": "issue",
                        "target_id": ref_id,
                    }
                )

        for label_name in _jira_labels(fields.get("labels")):
            _link_label(labels_by_name, label_links, issue_id, label_name)

        # Components become plain labels (no Athena component model in V1).
        for component_name in _jira_component_names(fields.get("components")):
            _link_label(
                labels_by_name,
                label_links,
                issue_id,
                f"component:{component_name}",
            )

        issue_type = _jira_issue_type_name(fields.get("issuetype"))
        if issue_type:
            _link_label(
                labels_by_name, label_links, issue_id, f"type:{issue_type}"
            )

        # fixVersions / versions become version: labels (no Athena release model in V1).
        for version_name in _jira_version_names(fields.get("fixVersions")):
            _link_label(
                labels_by_name, label_links, issue_id, f"version:{version_name}"
            )
        for version_name in _jira_version_names(fields.get("versions")):
            _link_label(
                labels_by_name, label_links, issue_id, f"version:{version_name}"
            )

        # Attachment metadata only — never copy blobs in the source mapper.
        for attachment in _jira_attachment_manifest_rows(fields.get("attachment")):
            report.note_attachment(attachment)

        for comment in _jira_comments(fields.get("comment")):
            author = _jira_user(comment.get("author") if isinstance(comment, dict) else None)
            comments.append(
                {
                    "id": comment_id,
                    "issue_id": issue_id,
                    "author_id": users.id_for(author),
                    "body": _body_to_text(comment.get("body") if isinstance(comment, dict) else ""),
                    "created_at": _string_or_now(
                        comment.get("created") if isinstance(comment, dict) else None,
                        now,
                    ),
                }
            )
            comment_id += 1

        for source_id, target_id, kind in _jira_links(issue, issue_id_by_key, report):
            if source_id == target_id:
                continue
            if kind == "relates" and source_id > target_id:
                source_id, target_id = target_id, source_id
            edge = (source_id, target_id, kind)
            if edge in link_seen:
                continue
            link_seen.add(edge)
            issue_links.append(
                {
                    "from_id": source_id,
                    "to_id": target_id,
                    "kind": kind,
                    "created_by": created_by,
                    "created_at": _string_or_now(fields.get("created"), now),
                }
            )

    # Real Jira dumps often list children under parent.fields.subtasks without
    # setting fields.parent on the child row. Backfill parent_id from that list.
    _backfill_parents_from_subtasks(issues_in, issue_rows, issue_id_by_key, report)

    # Backstop: after scoping to one project, no two issues should share a project_seq.
    # If they still do (a corrupt export with duplicate keys within one project), fail
    # loudly rather than emit a bundle whose [[KEY]] cross-refs resolve nondeterministically.
    seqs = [row["project_seq"] for row in issue_rows]
    if len(seqs) != len(set(seqs)):
        raise ValueError(
            "jira-project export produced duplicate issue keys within one project "
            "(colliding project_seq); the source appears to contain duplicate keys"
        )

    labels = sorted(labels_by_name.values(), key=lambda row: row["id"])
    project["created_by"] = owner_id
    project["created_at"] = min((row["created_at"] for row in issue_rows), default=now)
    project["issue_counter"] = max((row["project_seq"] or 0 for row in issue_rows), default=0)
    report.set_count("issues", len(issue_rows))
    report.set_count("comments", len(comments))
    report.set_count("issue_links", len(issue_links))
    report.set_count("cross_links", len(cross_links))
    report.set_count("labels", len(labels))
    report.set_count("users", len(users.rows()))
    report.set_count("attachment_manifest", len(report.attachment_manifest))

    bundle = _bundle_base("project", project_id, "jira-project", now) | {
        "project": project,
        "statuses": statuses,
        "members": [],
        "sprints": [],
        "issues": issue_rows,
        "comments": comments,
        "contributors": [],
        "issue_links": issue_links,
        "labels": labels,
        "label_links": _dedupe_dicts(label_links, ("issue_id", "label_id")),
        # Blobs stay empty until an attachment policy exists; the mapping report
        # carries a name/url/size manifest so operators know what was skipped.
        "attachments": [],
        "cross_links": _dedupe_dicts(
            cross_links,
            ("source_kind", "source_id", "target_kind", "target_id"),
        ),
        "activity": [],
        "users": users.rows(),
    }
    bundle["source"]["mapping_report"] = report.as_dict()
    return bundle


def map_confluence_space(
    payload: Any,
    *,
    space_key: str | None = None,
    space_name: str | None = None,
) -> dict:
    """Map a Confluence content/search JSON object into a space bundle."""
    pages_in = _extract_rows(
        payload,
        "results",
        "pages",
        "values",
        nested_paths=(
            ("data", "results"),
            ("data", "pages"),
            ("content", "results"),
            ("page", "results"),
        ),
    )
    if not pages_in:
        raise ValueError("confluence-space payload contains no pages")

    now = _utc_now()
    report = _MappingReport("confluence-space", now)
    users = _UserTable(now)
    # Derive space + owner from the first PAGE OBJECT, not pages_in[0] blindly — a leading
    # non-object row (a JSON null) would otherwise crash _confluence_space_info/
    # _confluence_created_by before the per-row guards below run.
    first_page = next((p for p in pages_in if isinstance(p, dict)), None)
    if first_page is None:
        raise ValueError("confluence-space payload contains no page objects")
    space = _confluence_space_info(first_page, space_key, space_name)
    space_id = 1
    owner_id = users.id_for(_confluence_created_by(first_page))

    # Assign page ids over the DICT rows only, in order, so a stray non-object row (a
    # JSON null, a string) is skipped and reported rather than crashing the whole map on
    # _source_id(...).get(). The main loop below skips the same rows, so len(pages_out)+1
    # stays in lockstep with these ids.
    page_id_by_source: dict[str, int] = {}
    page_id_by_title: dict[str, int] = {}
    next_page_id = 0
    for page in pages_in:
        if not isinstance(page, dict):
            report.note_unmapped("pages[]", "non-object page row was skipped")
            continue
        next_page_id += 1
        page_id_by_source[_source_id(page, fallback=str(next_page_id))] = next_page_id
        title = str(page.get("title") or "").casefold()
        if title:
            page_id_by_title.setdefault(title, next_page_id)

    labels_by_name: dict[str, dict] = {}
    label_links: list[dict] = []
    pages_out: list[dict] = []
    versions: list[dict] = []
    comments: list[dict] = []
    cross_links: list[dict] = []
    version_id = 1
    comment_id = 1

    for page in pages_in:
        if not isinstance(page, dict):
            continue
        _report_confluence_unmapped_fields(report, page)
        source_id = _source_id(page, fallback=str(len(pages_out) + 1))
        page_id = page_id_by_source[source_id]
        created_by = users.id_for(_confluence_created_by(page))
        updated_by = users.id_for(_confluence_updated_by(page))
        body_source = _confluence_body_source(page)
        storage_refs = _confluence_storage_ref_ids(
            body_source,
            page_id_by_source,
            page_id_by_title,
            report,
        )
        body = _rewrite_confluence_page_refs(
            _html_to_text(body_source),
            page_id_by_source,
            page_id_by_title,
        )
        body = _append_missing_page_refs(body, storage_refs)
        parent_id = _confluence_parent_id(page, page_id_by_source)
        if parent_id == page_id:
            # A page that lists itself as its own parent would seed a cycle the mentor
            # move-guard (validate_move) walks forever; drop the self-edge.
            parent_id = None
            report.note_unmapped(
                "pages[].ancestors", "page listed itself as its own parent"
            )
        if _confluence_has_unmapped_parent(page, page_id_by_source):
            report.note_unmapped(
                "pages[].ancestors",
                "parent or ancestor page is outside this source export",
            )

        pages_out.append(
            {
                "id": page_id,
                "space_id": space_id,
                "parent_id": parent_id,
                "title": str(page.get("title") or f"Page {page_id}"),
                "body": body,
                "created_by": created_by,
                "created_at": _string_or_now(_confluence_created_at(page), now),
                "updated_by": updated_by,
                "updated_at": _string_or_now(_confluence_updated_at(page), now),
            }
        )

        for target_id in _confluence_body_refs(body_source, body, page_id_by_source):
            if target_id != page_id:
                cross_links.append(
                    {
                        "source_kind": "page",
                        "source_id": page_id,
                        "target_kind": "page",
                        "target_id": target_id,
                    }
                )

        for version in _confluence_versions(page):
            versions.append(
                {
                    "id": version_id,
                    "page_id": page_id,
                    "version": int(version.get("number") or version.get("version") or version_id),
                    "title": str(version.get("title") or page.get("title") or f"Page {page_id}"),
                    "body": _rewrite_confluence_page_refs(
                        _html_to_text(_confluence_version_body(version)),
                        page_id_by_source,
                        page_id_by_title,
                    ),
                    "edited_by": users.id_for(_confluence_user(version.get("by") or version.get("author"))),
                    "created_at": _string_or_now(
                        version.get("when") or version.get("created_at"),
                        now,
                    ),
                }
            )
            version_id += 1

        for label_name in _confluence_labels(page):
            label = labels_by_name.setdefault(
                label_name.casefold(),
                {
                    "id": len(labels_by_name) + 1,
                    "name": label_name,
                    "color": _DEFAULT_LABEL_COLOR,
                },
            )
            label_links.append({"page_id": page_id, "label_id": label["id"]})

        for comment in _confluence_comments(page):
            comments.append(
                {
                    "id": comment_id,
                    "page_id": page_id,
                    "author_id": users.id_for(_confluence_user(comment.get("author") or comment.get("created_by"))),
                    "body": _html_to_text(comment.get("body") or comment.get("value") or ""),
                    "created_at": _string_or_now(
                        comment.get("created_at") or comment.get("createdDate"),
                        now,
                    ),
                }
            )
            comment_id += 1

    # A buggy/hand-edited export can list pages as each other's ancestors (A->B->A); the
    # self-edge is dropped above, but a longer cycle would still hang mentor's
    # validate_move (it walks the parent chain with no visited-set). Break any remaining
    # cycle so the imported tree is acyclic.
    _break_page_parent_cycles(pages_out, report)

    labels = sorted(labels_by_name.values(), key=lambda row: row["id"])
    space["created_by"] = owner_id
    space["created_at"] = min((row["created_at"] for row in pages_out), default=now)
    report.set_count("pages", len(pages_out))
    report.set_count("versions", len(versions))
    report.set_count("comments", len(comments))
    report.set_count("cross_links", len(cross_links))
    report.set_count("labels", len(labels))
    report.set_count("users", len(users.rows()))

    bundle = _bundle_base("space", space_id, "confluence-space", now) | {
        "space": space,
        "members": [],
        "pages": pages_out,
        "versions": versions,
        "comments": comments,
        "labels": labels,
        "label_links": _dedupe_dicts(label_links, ("page_id", "label_id")),
        "attachments": [],
        "cross_links": _dedupe_dicts(
            cross_links,
            ("source_kind", "source_id", "target_kind", "target_id"),
        ),
        "activity": [],
        "users": users.rows(),
    }
    bundle["source"]["mapping_report"] = report.as_dict()
    return bundle


def _break_page_parent_cycles(pages_out: list[dict], report: _MappingReport) -> None:
    """Ensure the page tree is acyclic: for each page, walk up its parent chain and, if
    it returns to the page itself, cut that page's parent edge (making it a root). A
    cycle among OTHER pages is left for whichever of its members reaches it — so a page
    with a legitimately deep chain keeps its parent."""
    by_id = {page["id"]: page for page in pages_out}
    for page in pages_out:
        seen = {page["id"]}
        node = by_id.get(page["parent_id"]) if page["parent_id"] is not None else None
        while node is not None:
            if node["id"] == page["id"]:
                page["parent_id"] = None  # page sits on its own ancestor chain → a cycle
                report.note_unmapped(
                    "pages[].ancestors", "cyclic parent chain was broken"
                )
                break
            if node["id"] in seen:
                break  # a cycle not involving `page`; broken from one of its own members
            seen.add(node["id"])
            node = by_id.get(node["parent_id"]) if node["parent_id"] is not None else None


def _bundle_base(kind: str, root_id: int, source: str, now: str) -> dict:
    return {
        "schema": portability.SCHEMA,
        "schema_version": 1,
        "kind": kind,
        "root_id": root_id,
        "exported_at": now,
        "source": {"kind": source, "mapped_at": now},
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"source is not valid JSON: {exc.msg}") from exc


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


def _extract_rows(
    payload: Any,
    *keys: str,
    nested_paths: tuple[tuple[str, ...], ...] = (),
) -> list:
    """Pull a row list from common export envelopes.

    Accepts a bare array, top-level keys (``issues``, ``results``, …), and a few
    nested shapes real Cloud/Server dumps use (``data.issues``, ``content.results``).
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("source payload must be a JSON object or array")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for path in nested_paths:
        cursor: Any = payload
        for part in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(part)
        if isinstance(cursor, list):
            return cursor
    labels = list(keys) + [".".join(path) for path in nested_paths]
    raise ValueError("source payload did not contain any of: " + ", ".join(labels))


def _jira_project_info(
    first_issue: dict,
    project_key: str | None,
    project_name: str | None,
) -> dict:
    fields = first_issue.get("fields") if isinstance(first_issue, dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    raw_key = project_key or project.get("key") or _jira_key_prefix(first_issue.get("key"))
    key = _clean_key(str(raw_key or "IMP"))
    return {
        "id": 1,
        "name": str(project_name or project.get("name") or key),
        "key": key,
        "description": str(project.get("description") or "Imported from Jira"),
        "created_by": 1,
        "created_at": _utc_now(),
        "issue_counter": 0,
        "visibility": "public",
    }


def _jira_issue_key(issue: dict, project_key: str, fallback_seq: int) -> str:
    raw = issue.get("key") if isinstance(issue, dict) else None
    if isinstance(raw, str) and _ISSUE_KEY_RE.match(raw):
        return raw.upper()
    return f"{project_key}-{fallback_seq}"


def _jira_key_prefix(key: Any) -> str | None:
    if not isinstance(key, str):
        return None
    match = _ISSUE_KEY_RE.match(key)
    return match.group(1) if match else None


def _jira_issue_seq(key: str, fallback: int) -> int:
    match = _ISSUE_KEY_RE.match(key)
    return int(match.group(2)) if match else fallback


def _field(issue: dict, name: str) -> Any:
    fields = issue.get("fields") if isinstance(issue, dict) else {}
    return fields.get(name) if isinstance(fields, dict) else None


def _jira_status_name(status: Any) -> str:
    if isinstance(status, dict):
        return str(status.get("name") or "open")
    return str(status or "open")


def _jira_status_category(status: Any) -> str:
    raw = ""
    if isinstance(status, dict):
        category = status.get("statusCategory")
        if isinstance(category, dict):
            raw = str(category.get("key") or category.get("name") or "")
        raw = raw or str(status.get("name") or "")
    raw = raw.casefold()
    if raw in {"done", "complete", "completed"} or "done" in raw:
        return "done"
    if raw in {"indeterminate", "doing", "progress"} or "progress" in raw:
        return "doing"
    return "todo"


def _jira_priority(priority: Any) -> str:
    name = ""
    if isinstance(priority, dict):
        name = str(priority.get("name") or "")
    elif priority is not None:
        name = str(priority)
    name = name.casefold()
    if any(token in name for token in ("highest", "critical", "blocker", "urgent")):
        return "urgent"
    if any(token in name for token in ("high", "major")):
        return "high"
    if any(token in name for token in ("low", "minor", "trivial")):
        return "low"
    return "medium"


def _jira_parent_key(parent: Any) -> str | None:
    if isinstance(parent, dict) and isinstance(parent.get("key"), str):
        return parent["key"].upper()
    return None


def _jira_labels(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    out = []
    for label in labels:
        name = str(label).strip()
        if name and name not in out:
            out.append(name)
    return out


def _link_label(
    labels_by_name: dict[str, dict],
    label_links: list[dict],
    issue_id: int,
    label_name: str,
) -> None:
    name = label_name.strip()
    if not name:
        return
    label = labels_by_name.setdefault(
        name.casefold(),
        {
            "id": len(labels_by_name) + 1,
            "name": name,
            "color": _DEFAULT_LABEL_COLOR,
        },
    )
    label_links.append({"issue_id": issue_id, "label_id": label["id"]})


def _jira_component_names(components: Any) -> list[str]:
    if not isinstance(components, list):
        return []
    out: list[str] = []
    for item in components:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out


def _jira_issue_type_name(issuetype: Any) -> str | None:
    if isinstance(issuetype, dict):
        name = str(issuetype.get("name") or "").strip()
        return name or None
    if issuetype is None:
        return None
    name = str(issuetype).strip()
    return name or None


def _jira_version_names(versions: Any) -> list[str]:
    """Extract release/version names from Jira fixVersions or versions arrays."""
    if not isinstance(versions, list):
        return []
    out: list[str] = []
    for item in versions:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out


def _backfill_parents_from_subtasks(
    issues_in: list,
    issue_rows: list[dict],
    issue_id_by_key: dict[str, int],
    report: "_MappingReport",
) -> None:
    """Set parent_id from parent.fields.subtasks when the child omits parent."""
    row_by_id = {row["id"]: row for row in issue_rows}
    filled = 0
    for issue in issues_in:
        if not isinstance(issue, dict):
            continue
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        subtasks = fields.get("subtasks")
        if not isinstance(subtasks, list) or not subtasks:
            continue
        parent_key = None
        raw_key = issue.get("key")
        if isinstance(raw_key, str) and _ISSUE_KEY_RE.match(raw_key):
            parent_key = raw_key.upper()
        parent_id = issue_id_by_key.get(parent_key) if parent_key else None
        if parent_id is None:
            continue
        for sub in subtasks:
            if not isinstance(sub, dict):
                continue
            child_key = sub.get("key")
            if not isinstance(child_key, str):
                continue
            child_id = issue_id_by_key.get(child_key.upper())
            if child_id is None:
                report.note_unmapped(
                    "issues[].fields.subtasks",
                    "subtask key is outside this source export",
                )
                continue
            child = row_by_id.get(child_id)
            if child is None:
                continue
            if child.get("parent_id") is None:
                child["parent_id"] = parent_id
                filled += 1
            elif child.get("parent_id") != parent_id:
                report.note_unmapped(
                    "issues[].fields.subtasks",
                    "subtask parent conflict between parent field and subtasks list",
                )
    if filled:
        report.set_count("parent_links_from_subtasks", filled)


def _jira_attachment_manifest_rows(attachment_field: Any) -> list[dict]:
    if not isinstance(attachment_field, list):
        return []
    rows: list[dict] = []
    for item in attachment_field:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or item.get("name") or "").strip()
        if not filename:
            continue
        size = item.get("size")
        try:
            size_int = int(size) if size is not None else None
        except (TypeError, ValueError):
            size_int = None
        rows.append(
            {
                "filename": filename,
                "size_bytes": size_int,
                "mime_type": str(item.get("mimeType") or item.get("mime_type") or "")
                or None,
                "content_url": str(item.get("content") or item.get("url") or "")
                or None,
                "note": "blob not imported — attachment policy required",
            }
        )
    return rows


def _jira_comments(comment_field: Any) -> list[dict]:
    if isinstance(comment_field, dict) and isinstance(comment_field.get("comments"), list):
        return [c for c in comment_field["comments"] if isinstance(c, dict)]
    if isinstance(comment_field, list):
        return [c for c in comment_field if isinstance(c, dict)]
    return []


def _jira_links(
    issue: dict,
    issue_id_by_key: dict[str, int],
    report: "_MappingReport",
) -> list[tuple[int, int, str]]:
    if not isinstance(issue, dict):
        return []
    current_key = _jira_issue_key(issue, "IMP", 0)
    current_id = issue_id_by_key.get(current_key)
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    rows = fields.get("issuelinks") if isinstance(fields, dict) else []
    if current_id is None or not isinstance(rows, list):
        return []

    out: list[tuple[int, int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        link_type = row.get("type") if isinstance(row.get("type"), dict) else {}
        if isinstance(row.get("outwardIssue"), dict):
            target = issue_id_by_key.get(str(row["outwardIssue"].get("key", "")).upper())
            if target is None:
                report.note_unmapped(
                    "issues[].fields.issuelinks",
                    "linked issue is outside this source export",
                )
                continue
            if _jira_link_is_blocks(link_type, outward=True):
                out.append((current_id, target, "blocks"))
            else:
                out.append((current_id, target, "relates"))
        if isinstance(row.get("inwardIssue"), dict):
            target = issue_id_by_key.get(str(row["inwardIssue"].get("key", "")).upper())
            if target is None:
                report.note_unmapped(
                    "issues[].fields.issuelinks",
                    "linked issue is outside this source export",
                )
                continue
            if _jira_link_is_blocks(link_type, outward=False):
                out.append((target, current_id, "blocks"))
            else:
                out.append((current_id, target, "relates"))
    return out


def _jira_link_is_blocks(link_type: dict, *, outward: bool) -> bool:
    words = [str(link_type.get("name") or "")]
    words.append(str(link_type.get("outward" if outward else "inward") or ""))
    return "block" in " ".join(words).casefold()


def _rewrite_jira_issue_keys(text: str, issue_id_by_key: dict[str, int]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(0).upper()
        issue_id = issue_id_by_key.get(key)
        return f"[[issue:{issue_id}]]" if issue_id is not None else match.group(0)

    return _ISSUE_KEY_RE.sub(replace, text)


def _jira_user(value: Any) -> dict | None:
    if value is None:
        return None
    return _user_from_mapping(value)


def _confluence_space_info(
    first_page: dict,
    space_key: str | None,
    space_name: str | None,
) -> dict:
    space = first_page.get("space") if isinstance(first_page, dict) else {}
    space = space if isinstance(space, dict) else {}
    key = _clean_key(str(space_key or space.get("key") or "DOC"))
    return {
        "id": 1,
        "key": key,
        "name": str(space_name or space.get("name") or key),
        "description": str(space.get("description") or "Imported from Confluence"),
        "created_by": 1,
        "created_at": _utc_now(),
        "visibility": "public",
    }


def _confluence_created_by(page: dict) -> dict | None:
    history = page.get("history") if isinstance(page.get("history"), dict) else {}
    return _confluence_user(history.get("createdBy") or page.get("createdBy"))


def _confluence_updated_by(page: dict) -> dict | None:
    version = page.get("version") if isinstance(page.get("version"), dict) else {}
    return _confluence_user(version.get("by") or page.get("lastUpdatedBy") or _confluence_created_by(page))


def _confluence_created_at(page: dict) -> Any:
    history = page.get("history") if isinstance(page.get("history"), dict) else {}
    return history.get("createdDate") or page.get("created_at")


def _confluence_updated_at(page: dict) -> Any:
    version = page.get("version") if isinstance(page.get("version"), dict) else {}
    return version.get("when") or page.get("updated_at") or _confluence_created_at(page)


def _confluence_body_source(page: dict) -> str:
    body = page.get("body")
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        for key in ("storage", "view", "export_view"):
            value = body.get(key)
            if isinstance(value, dict) and isinstance(value.get("value"), str):
                return value["value"]
        if isinstance(body.get("value"), str):
            return body["value"]
    return ""


def _confluence_parent_id(page: dict, page_id_by_source: dict[str, int]) -> int | None:
    parent = page.get("parent_id") or page.get("parentId")
    if parent is not None:
        mapped = page_id_by_source.get(str(parent))
        if mapped is not None:
            return mapped
    ancestors = page.get("ancestors")
    if isinstance(ancestors, list) and ancestors:
        for ancestor in reversed(ancestors):
            if isinstance(ancestor, dict):
                mapped = page_id_by_source.get(_source_id(ancestor, fallback=""))
                if mapped is not None:
                    return mapped
    return None


def _confluence_has_unmapped_parent(
    page: dict,
    page_id_by_source: dict[str, int],
) -> bool:
    parent = page.get("parent_id") or page.get("parentId")
    if parent is not None and str(parent) not in page_id_by_source:
        return True
    ancestors = page.get("ancestors")
    if isinstance(ancestors, list) and ancestors:
        return not any(
            isinstance(ancestor, dict)
            and _source_id(ancestor, fallback="") in page_id_by_source
            for ancestor in ancestors
        )
    return False


def _confluence_versions(page: dict) -> list[dict]:
    versions = page.get("versions")
    if isinstance(versions, list):
        return [v for v in versions if isinstance(v, dict)]
    return []


def _confluence_version_body(version: dict) -> str:
    body = version.get("body")
    if isinstance(body, dict):
        value = body.get("storage") or body.get("view") or body
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            return value["value"]
    return str(body or "")


def _confluence_labels(page: dict) -> list[str]:
    metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
    labels = page.get("labels") or metadata.get("labels")
    if isinstance(labels, dict):
        labels = labels.get("results")
    if not isinstance(labels, list):
        return []
    out = []
    for label in labels:
        if isinstance(label, dict):
            name = str(label.get("name") or label.get("label") or "").strip()
        else:
            name = str(label).strip()
        if name and name not in out:
            out.append(name)
    return out


def _confluence_comments(page: dict) -> list[dict]:
    comments = page.get("comments")
    if isinstance(comments, dict):
        comments = comments.get("results")
    if isinstance(comments, list):
        return [c for c in comments if isinstance(c, dict)]
    return []


def _confluence_body_refs(
    source: str,
    rewritten_body: str,
    page_id_by_source: dict[str, int],
) -> set[int]:
    refs = {ref_id for kind, ref_id in links.extract_refs(rewritten_body) if kind == "page"}
    for match in _RESOURCE_ID_RE.findall(source):
        mapped = page_id_by_source.get(match)
        if mapped is not None:
            refs.add(mapped)
    return refs


def _confluence_storage_ref_ids(
    source: str,
    page_id_by_source: dict[str, int],
    page_id_by_title: dict[str, int],
    report: "_MappingReport",
) -> set[int]:
    refs: set[int] = set()
    for raw_id in [*_RESOURCE_ID_RE.findall(source), *_RI_CONTENT_ID_RE.findall(source)]:
        mapped = page_id_by_source.get(raw_id)
        if mapped is None:
            report.note_unmapped(
                "pages[].body.storage.links",
                "linked page id is outside this source export",
            )
            continue
        refs.add(mapped)
    for raw_title in _RI_CONTENT_TITLE_RE.findall(source):
        mapped = page_id_by_title.get(unescape(raw_title).casefold())
        if mapped is None:
            report.note_unmapped(
                "pages[].body.storage.links",
                "linked page title is outside this source export",
            )
            continue
        refs.add(mapped)
    return refs


def _append_missing_page_refs(body: str, refs: set[int]) -> str:
    if not refs:
        return body
    existing = {ref_id for kind, ref_id in links.extract_refs(body) if kind == "page"}
    missing = [ref_id for ref_id in sorted(refs) if ref_id not in existing]
    if not missing:
        return body
    suffix = " ".join(f"[[page:{ref_id}]]" for ref_id in missing)
    return f"{body}\n{suffix}".strip() if body else suffix


def _rewrite_confluence_page_refs(
    text: str,
    page_id_by_source: dict[str, int],
    page_id_by_title: dict[str, int],
) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        mapped = page_id_by_source.get(raw) or page_id_by_title.get(raw.casefold())
        return f"[[page:{mapped}]]" if mapped is not None else match.group(0)

    return _PAGE_TOKEN_RE.sub(replace, text)


def _confluence_user(value: Any) -> dict | None:
    if value is None:
        return None
    return _user_from_mapping(value)


def _report_jira_unmapped_fields(report: "_MappingReport", issue: Any) -> None:
    if not isinstance(issue, dict):
        report.note_unmapped("issues[]", "non-object issue row was skipped")
        return
    _note_unmapped_keys(
        report,
        "issues[]",
        issue,
        _JIRA_ISSUE_KEYS,
        "Jira issue property is not mapped into Athena V1 bundles",
    )
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return
    for key in sorted(set(fields) - _JIRA_FIELD_KEYS):
        report.note_unmapped(f"issues[].fields.{key}", _jira_unmapped_field_reason(key))


def _jira_unmapped_field_reason(key: str) -> str:
    if key == "attachment":
        return (
            "attachment blobs are not imported; names/sizes land in "
            "mapping_report.attachment_manifest"
        )
    if key.startswith("customfield_"):
        return "custom Jira fields are not mapped into Athena V1 fields"
    if key in {"updated", "resolution", "resolutiondate", "changelog"}:
        return "Jira resolution/update/changelog metadata is not mapped into Athena V1 fields"
    return "Jira field is not mapped into Athena V1 fields"


def _report_confluence_unmapped_fields(report: "_MappingReport", page: Any) -> None:
    if not isinstance(page, dict):
        report.note_unmapped("pages[]", "non-object page row was skipped")
        return
    for key in sorted(set(page) - _CONFLUENCE_PAGE_KEYS):
        report.note_unmapped(f"pages[].{key}", _confluence_unmapped_field_reason(key))


def _confluence_unmapped_field_reason(key: str) -> str:
    if key in {"restrictions", "operations"}:
        return "Confluence permissions/operations are not mapped into Athena V1 bundles"
    if key in {"_links", "_expandable", "extensions"}:
        return "Confluence presentation metadata is not mapped into Athena V1 bundles"
    if key in {"descendants", "children"}:
        return "Confluence child expansion metadata is not mapped into Athena V1 bundles"
    return "Confluence page property is not mapped into Athena V1 bundles"


def _note_unmapped_keys(
    report: "_MappingReport",
    prefix: str,
    row: dict,
    known: set[str],
    reason: str,
) -> None:
    for key in sorted(set(row) - known):
        report.note_unmapped(f"{prefix}.{key}", reason)


def _source_id(row: dict, *, fallback: str) -> str:
    value = row.get("id") or row.get("contentId") or row.get("key") or fallback
    return str(value)


def _user_from_mapping(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"email": value if "@" in value else f"{_slug(value)}@import.local", "name": value}
    if not isinstance(value, dict):
        return None
    email = (
        value.get("emailAddress")
        or value.get("email")
        or value.get("accountEmail")
        or value.get("username")
    )
    name = value.get("displayName") or value.get("publicName") or value.get("name")
    account = value.get("accountId") or value.get("userKey") or value.get("key")
    if not email or "@" not in str(email):
        email = f"{_slug(str(account or name or 'user'))}@import.local"
    return {"email": str(email), "name": str(name or email)}


def _body_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _adf_to_text(value).strip()
    if isinstance(value, list):
        return "\n".join(_body_to_text(item) for item in value).strip()
    return str(value)


def _adf_to_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        return str(attrs.get("text") or attrs.get("displayName") or "")
    children = [_adf_to_text(child) for child in node.get("content", []) if child is not None]
    separator = "\n\n" if node_type in {"doc", "paragraph", "bulletList", "orderedList", "listItem"} else ""
    return separator.join(part for part in children if part)


def _html_to_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("storage", "view", "export_view"):
            nested = value.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("value"), str):
                value = nested["value"]
                break
        else:
            if isinstance(value.get("value"), str):
                value = value["value"]
    text = _body_to_text(value)
    if "<" not in text or ">" not in text:
        return unescape(text)
    parser = _TextHTMLParser()
    parser.feed(text)
    parser.close()
    return parser.text()


# Tags that start/end a text block, so their content lands on its own line rather than
# run together with a neighbour. td/th are included so adjacent table cells don't
# concatenate ("AB" from <td>A</td><td>B</td>).
_BLOCK_TAGS = {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4"}


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def unknown_decl(self, data: str) -> None:
        # Confluence storage format wraps code/noformat macro bodies in
        # <ac:plain-text-body><![CDATA[...]]></ac:plain-text-body>. HTMLParser routes a
        # CDATA section here as "CDATA[<content>"; the base class then DROPS it, so a
        # runbook's code block vanishes silently on import. Capture the content (as plain
        # text — the macro's language/formatting is flattened, but the code itself is
        # preserved rather than lost) on its own lines.
        if data.startswith("CDATA["):
            self._parts.append("\n" + data[len("CDATA["):] + "\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        raw = unescape("".join(self._parts))
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


class _MappingReport:
    def __init__(self, source_kind: str, generated_at: str) -> None:
        self._source_kind = source_kind
        self._generated_at = generated_at
        self._counts: dict[str, int] = {}
        self._unmapped: dict[tuple[str, str], int] = {}
        self.attachment_manifest: list[dict] = []

    def set_count(self, name: str, value: int) -> None:
        self._counts[name] = value

    def note_unmapped(self, path: str, reason: str) -> None:
        key = (path, reason)
        self._unmapped[key] = self._unmapped.get(key, 0) + 1

    def note_attachment(self, row: dict) -> None:
        """Record attachment metadata without importing the blob."""
        self.attachment_manifest.append(row)

    def as_dict(self) -> dict:
        unmapped_fields = [
            {"path": path, "reason": reason, "count": count}
            for (path, reason), count in sorted(self._unmapped.items())
        ]
        gaps = bool(unmapped_fields) or bool(self.attachment_manifest)
        return {
            "schema": SOURCE_MAPPING_REPORT_SCHEMA,
            "schema_version": 1,
            "source_kind": self._source_kind,
            "generated_at": self._generated_at,
            "status": "mapped_with_gaps" if gaps else "mapped",
            "counts": dict(sorted(self._counts.items())),
            "unmapped_fields": unmapped_fields,
            # Name/size/url only — never file contents. Operators use this to
            # decide what still needs a manual blob policy.
            "attachment_manifest": list(self.attachment_manifest),
        }


class _UserTable:
    def __init__(self, now: str) -> None:
        self._now = now
        self._rows: list[dict] = []
        self._by_email: dict[str, int] = {}

    def id_for(self, user: dict | None) -> int:
        if user is None:
            user = {"email": _IMPORT_USER_EMAIL, "name": _IMPORT_USER_NAME}
        email = str(user["email"]).strip().casefold()
        existing = self._by_email.get(email)
        if existing is not None:
            return existing
        user_id = len(self._rows) + 1
        self._by_email[email] = user_id
        self._rows.append(
            {
                "id": user_id,
                "email": str(user["email"]).strip(),
                "name": str(user.get("name") or user["email"]).strip(),
                "role": "member",
                "is_agent": False,
                "created_at": self._now,
            }
        )
        return user_id

    def rows(self) -> list[dict]:
        return list(self._rows)


def _clean_key(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"IMP{cleaned}"
    cleaned = cleaned[:10]
    return cleaned if _ATHENA_KEY_RE.match(cleaned) else "IMP"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "user"


def _string_or_now(value: Any, now: str) -> str:
    return str(value) if isinstance(value, str) and value else now


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _dedupe_dicts(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        ident = tuple(row[key] for key in keys)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(row)
    return out
