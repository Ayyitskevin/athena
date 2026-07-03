"""Import bundles are bounded before replay, so a hostile or oversized input is
refused up front rather than materialized one INSERT at a time under the write lock."""

import pytest

from athena.core import db, portability


def _skeletal_bundle(*, issue_count: int) -> dict:
    # The minimum a bundle needs to clear _validate_bundle's field checks and reach the
    # row-count guard: a valid schema/version/kind/root_id/exported_at plus one array.
    return {
        "schema": portability.SCHEMA,
        "schema_version": 1,
        "kind": "project",
        "root_id": 1,
        "exported_at": "2026-01-01T00:00:00+00:00",
        "issues": [{"id": i} for i in range(issue_count)],
    }


def test_import_refuses_a_bundle_with_too_many_rows(tmp_path, monkeypatch):
    # WHY: a bundle is imported by replaying one INSERT per row inside a single write
    # transaction, so an enormous total row count is a memory / lock-hold hazard. The cap
    # lives in _validate_bundle so it covers every in-process import path — not only the
    # CLI's separate on-disk file-size guard, which an in-memory bundle skips entirely.
    # Shrink the cap so the test stays a handful of rows instead of half a million.
    monkeypatch.setattr(portability, "_MAX_BUNDLE_ROWS", 3)
    conn = db.connect(tmp_path / "bounds.db")
    db.migrate(conn)
    try:
        with pytest.raises(ValueError, match="too many rows"):
            portability.build_import_manifest(conn, _skeletal_bundle(issue_count=5))
    finally:
        conn.close()


def test_validate_bundle_allows_a_bundle_at_the_row_cap(monkeypatch):
    # The cap is a ceiling, not an off-by-one trip: a bundle with exactly _MAX_BUNDLE_ROWS
    # rows passes the guard unchanged, so the bound can never reject a legitimate migration
    # that fits inside it. Checked at _validate_bundle directly (independent of whatever
    # the skeletal bundle would later trip on downstream).
    monkeypatch.setattr(portability, "_MAX_BUNDLE_ROWS", 3)
    bundle = _skeletal_bundle(issue_count=3)
    assert portability._validate_bundle(bundle) is bundle
