"""The consolidated core helpers (was copy-pasted across portability / source_import /
run_replay / sessions / webhooks). Pins the contract the callers rely on."""

import json
from datetime import timezone

from athena.core import _util


def test_atomic_write_json_round_trips_privately_without_fixed_temp(tmp_path):
    dest = tmp_path / "nested" / "out.json"
    dest.parent.mkdir()
    fixed_temp_sentinel = dest.parent / f".{dest.name}.tmp"
    fixed_temp_sentinel.write_bytes(b"unrelated")

    _util.atomic_write_json(dest, {"b": 1, "a": 2})

    text = dest.read_text(encoding="utf-8")
    assert text == ('{\n  "a": 2,\n  "b": 1\n}\n')
    assert json.loads(text) == {"a": 2, "b": 1}
    assert dest.stat().st_mode & 0o777 == 0o600
    assert fixed_temp_sentinel.read_bytes() == b"unrelated"
    assert not list(dest.parent.glob(f".{dest.name}.*.tmp"))


def test_atomic_write_json_leaves_original_on_failure(tmp_path):
    dest = tmp_path / "out.json"
    _util.atomic_write_json(dest, {"ok": 1})
    original = dest.read_text(encoding="utf-8")
    # A non-serializable payload raises AND leaves the prior file intact (atomic).
    try:
        _util.atomic_write_json(dest, {"bad": object()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError on non-serializable data")
    assert dest.read_text(encoding="utf-8") == original
    assert not (dest.parent / f".{dest.name}.tmp").exists()


def test_utc_now_is_timezone_aware_utc():
    now = _util.utc_now()
    assert now.tzinfo is not None and now.utcoffset() == timezone.utc.utcoffset(None)


def test_utc_now_iso_is_second_precision_and_zulu():
    s = _util.utc_now_iso()
    # e.g. 2026-07-12T21:30:00Z — ends in Z (not +00:00), no sub-second component.
    assert s.endswith("Z") and "+00:00" not in s and "." not in s
