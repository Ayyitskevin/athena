"""Map saved wiki-link identities across a selective project's or space's ID boundary."""

from __future__ import annotations

from collections import defaultdict
import sqlite3

from athena.core import links


def _sources(bundle: dict) -> tuple[str, list[dict]]:
    if bundle["kind"] == "space":
        return "page", bundle["pages"]
    return "issue", bundle["issues"]


def export(conn: sqlite3.Connection, bundle: dict) -> None:
    """Include token provenance only for sources inside this export.

    An external or deleted target becomes an explicitly unlinked token. Keeping its
    old database-local ID would risk opening an unrelated destination page.
    """
    # The offline exporter can open a pre-binding database read-only. Leave the
    # optional field absent so the importer uses the original V1 graph evidence.
    if not conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'title_link_bindings'"
    ).fetchone():
        return
    kind, sources = _sources(bundle)
    page_ids = {row["id"] for row in bundle.get("pages", [])}
    rows = []
    for source in sources:
        if not links.title_keys(source.get("body")):
            continue
        saved = links.stored_title_bindings(conn, kind, source["id"])
        for token, target in sorted(saved.items()):
            rows.append(
                {
                    "source_id": source["id"],
                    "token": token,
                    "target_page_id": target if target in page_ids else None,
                }
            )
    bundle["title_bindings"] = rows


def _page_edges(bundle: dict, source_kind: str) -> dict[int, set[int]]:
    edges: dict[int, set[int]] = defaultdict(set)
    for edge in bundle["cross_links"]:
        if edge["source_kind"] == source_kind and edge["target_kind"] == "page":
            edges[edge["source_id"]].add(edge["target_id"])
    return edges


def validate(bundle: dict) -> None:
    """Check metadata against the authored text and the bundled graph before replay."""
    if "title_bindings" not in bundle:
        return
    rows = bundle["title_bindings"]
    if not isinstance(rows, list):
        raise ValueError("bundle.title_bindings must be a list")
    if not rows:
        return
    kind, sources = _sources(bundle)
    authored = {row["id"]: links.title_keys(row.get("body")) for row in sources}
    internal_pages = set(authored) if kind == "page" else set()
    edges = _page_edges(bundle, kind)
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or "target_page_id" not in row:
            raise ValueError("title_bindings row must include target_page_id")
        source, token, target = (
            row.get("source_id"),
            row.get("token"),
            row["target_page_id"],
        )
        if type(source) is not int or source not in authored:
            raise ValueError("title_bindings source must be a bundled source ID")
        if not isinstance(token, str) or token not in authored[source]:
            raise ValueError("title_bindings token must appear in the source body")
        if (source, token) in seen:
            raise ValueError("duplicate title_bindings token for source")
        seen.add((source, token))
        if target is None:
            continue
        if type(target) is not int or target not in internal_pages:
            raise ValueError("title_bindings target must be a bundled page ID")
        if target != source and target not in edges.get(source, ()):
            raise ValueError("title_bindings target must match a bundled page edge")


def restore(conn: sqlite3.Connection, bundle: dict, source_map: dict[int, int]) -> None:
    """Install source-owned bindings inside the caller's replay transaction."""
    kind, sources = _sources(bundle)
    page_map = source_map if kind == "page" else {}
    saved: dict[int, dict[str, int | None]] = defaultdict(dict)
    if "title_bindings" in bundle:
        for row in bundle["title_bindings"]:
            saved[row["source_id"]][row["token"]] = row["target_page_id"]
    else:
        # Reconcile old-format bundles BEFORE dropping external edges or changing IDs.
        edges = _page_edges(bundle, kind)
        titles: dict[int, set[str]] = defaultdict(set)
        if kind == "page":
            for page in sources:
                titles[page["id"]].add(page["title"])
            for version in bundle["versions"]:
                titles[version["page_id"]].add(version["title"])
        for source in sources:
            saved[source["id"]] = links.legacy_title_bindings(
                source.get("body"), edges.get(source["id"], set()), titles_by_id=titles
            )
    for old_source, bindings in saved.items():
        if bindings:
            mapped = {
                token: page_map.get(target) if target is not None else None
                for token, target in bindings.items()
            }
            links.restore_title_bindings(conn, kind, source_map[old_source], mapped)
