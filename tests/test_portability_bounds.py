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


def test_bounded_verb_strips_control_chars_truncates_and_defaults():
    # An imported verb comes from an untrusted bundle. Strip non-printable chars (so the
    # trail can't be laced with control bytes), cap the length, and never store an empty
    # verb — a clean native-style verb passes through untouched.
    assert portability._bounded_verb("a\x07b\tc") == "abc"
    assert portability._bounded_verb("x" * 200) == "x" * 80
    assert portability._bounded_verb("triaged") == "triaged"
    for empty in ("", "   ", "\n\t", None, 123):
        assert portability._bounded_verb(empty) == "imported"


def test_bounded_created_at_keeps_honest_past_and_clamps_the_rest():
    from datetime import datetime

    ceiling = datetime(2026, 1, 1, 0, 0, 0)
    fb = "IMPORT-INSTANT"
    # An honest past timestamp survives verbatim — migrated history keeps its own time.
    assert (
        portability._bounded_created_at("2020-05-05 12:00:00", fb, ceiling)
        == "2020-05-05 12:00:00"
    )
    # Everything a hostile or corrupt bundle can carry clamps to the import instant:
    assert portability._bounded_created_at("3000-01-01 00:00:00", fb, ceiling) == fb  # future
    assert portability._bounded_created_at("not-a-date", fb, ceiling) == fb  # unparseable
    assert portability._bounded_created_at(None, fb, ceiling) == fb  # non-string
    assert portability._bounded_created_at(1234567890, fb, ceiling) == fb  # non-string
