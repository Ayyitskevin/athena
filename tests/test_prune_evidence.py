"""The prune ledger's evidence collector has to be right about its own map.

`scripts/prune_evidence.py` answers "when was this subsystem last really used?" by
counting activity events carrying that subsystem's verbs. A verb name that the code
never writes therefore reports **zero** — indistinguishable from a genuinely dead
feature, and pointing the same direction: cut it.

That is not hypothetical. The first draft of the map guessed verb names, and
reported zero events for agent supervision against a demo database that plainly
exercises supervision. Nothing failed; the number was simply wrong, and it argued
for cutting the fleet loop — the feature VISION names as the whole point.

So the map is pinned against the vocabulary the code actually writes. These tests
are cheap and the failure they prevent is a subsystem deleted on false evidence.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# Registered in sys.modules BEFORE exec_module, matching the house pattern in
# test_supply_chain.py: `@dataclass` resolves its annotations through
# `sys.modules[cls.__module__]`, so a module that has not been registered yet
# fails with an opaque AttributeError on None.
_SPEC = importlib.util.spec_from_file_location(
    "athena_prune_evidence", REPO_ROOT / "scripts" / "prune_evidence.py"
)
assert _SPEC is not None and _SPEC.loader is not None
prune_evidence = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = prune_evidence
_SPEC.loader.exec_module(prune_evidence)


def _verbs_written_by_the_code() -> set[str]:
    """Every activity verb literal the source can write.

    AST-based rather than regex, because the codebase spells verbs four ways and
    two of them defeat a regex outright:

      * `verb="created"` — the easy one;
      * `VERB_LOGIN_THROTTLED = "login_throttled"` — a module constant;
      * `VERBS = {KIND_PUSH: "forge_commit", ...}` — a dict (forge);
      * `verb="lease_renewed" if renewed else "claimed"` — a CONDITIONAL, where a
        regex sees the first branch and silently loses the second.

    That last form is not hypothetical either: `claimed` is written exactly that
    way, and a regex-based version of this helper declared it non-existent while
    the demo database contained it.
    """
    verbs: set[str] = set()

    def _strings(node: ast.AST) -> set[str]:
        """Every string constant a value expression can evaluate to."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return _strings(node.body) | _strings(node.orelse)
        if isinstance(node, (ast.Dict,)):
            return {
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            }
        return set()

    for path in (REPO_ROOT / "src" / "athena").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "verb":
                        verbs |= _strings(keyword.value)
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any("VERB" in name.upper() for name in names):
                    verbs |= _strings(node.value)
    return {v for v in verbs if v.replace("_", "").isalpha()}


def test_every_verb_in_the_map_is_one_the_code_actually_writes():
    """The failure this file exists for. A verb nobody writes counts zero, and zero
    reads as 'dead' to whoever is deciding what to cut."""
    written = _verbs_written_by_the_code()
    assert len(written) > 100, "verb extraction broke; it should find ~123"
    unknown = {
        (subsystem.name, verb)
        for subsystem in prune_evidence.SUBSYSTEMS
        for verb in subsystem.verbs
        if verb not in written
    }
    assert not unknown, (
        "these verbs are named in the prune map but never written by the code, so "
        f"they would report zero use: {sorted(unknown)}"
    )


def test_every_table_in_the_map_exists_in_the_schema():
    """Same failure, one column over: a renamed table would silently drop out of the
    row count rather than announcing itself."""
    from athena.core import db

    import tempfile

    path = pathlib.Path(tempfile.mkdtemp()) / "schema.db"
    conn = db.connect(path)
    db.migrate(conn)
    try:
        present = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
    finally:
        conn.close()
    missing = {
        (subsystem.name, table)
        for subsystem in prune_evidence.SUBSYSTEMS
        for table in subsystem.tables
        if table not in present
    }
    assert not missing, (
        f"tables named in the prune map are not in the schema: {sorted(missing)}"
    )


def test_a_subsystem_that_leaves_no_trace_is_reported_as_na_not_zero():
    """The distinction the whole report rests on. `n/a` means "this script cannot
    see it"; `0` means "measurable, and nobody used it". Collapsing them would let a
    read-only surface look exactly like a dead one."""
    unmeasurable = [s for s in prune_evidence.SUBSYSTEMS if s.read_only]
    assert unmeasurable, "the map should contain read-only surfaces"
    for subsystem in unmeasurable:
        finding = prune_evidence.Finding(
            subsystem=subsystem,
            table_rows={},
            missing_tables=(),
            verb_events=0,
            last_seen=None,
        )
        assert not finding.measurable
        assert "n/a" in finding.evidence
        assert "0" not in finding.evidence


def test_forge_counts_imported_events_because_that_is_how_forge_records():
    """The bug that forced `counts_imported` to exist.

    Forge deliveries land as imported history by design — the module says so: every
    landed row is "Athena's record of what it was told". The default evidence query
    excludes imported rows (they are usually another deployment's history), so forge
    reported zero however much it was used, arguing to cut a working subsystem.
    """
    forge = next(s for s in prune_evidence.SUBSYSTEMS if s.name == "forge inbound")
    assert forge.counts_imported, "forge evidence must include imported rows"

    from athena.aegis import forge as forge_module

    for verb in forge_module.VERBS.values():
        assert verb in forge.verbs, (
            f"{verb} is a forge landing verb and must be counted"
        )

    # ...and everything else still excludes them, so an imported bundle cannot
    # inflate another subsystem's apparent use.
    others = [s for s in prune_evidence.SUBSYSTEMS if s.name != "forge inbound"]
    assert not any(s.counts_imported for s in others)


def test_the_report_runs_against_a_real_database(tmp_path):
    from athena.core import db

    path = tmp_path / "report.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()

    reader = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    reader.row_factory = sqlite3.Row
    try:
        findings = prune_evidence.collect(reader)
    finally:
        reader.close()

    assert len(findings) == len(prune_evidence.SUBSYSTEMS)
    # A migrated-but-unused database: every measurable subsystem reads zero, and no
    # table is missing, which is what makes the zeros trustworthy.
    for finding in findings:
        assert finding.missing_tables == ()
        if finding.measurable:
            assert finding.total_rows == 0
            assert finding.verb_events == 0


@pytest.mark.parametrize("flag", [[], ["--markdown"]])
def test_the_cli_runs_in_both_shapes(tmp_path, capsys, flag):
    from athena.core import db

    path = tmp_path / "cli.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()

    assert prune_evidence.main([str(path), *flag]) == 0
    out = capsys.readouterr().out
    assert "issues (Aegis core)" in out
    if flag:
        assert out.count("|") > len(prune_evidence.SUBSYSTEMS)


def test_a_missing_database_is_refused_rather_than_reported_as_empty(tmp_path, capsys):
    """An empty report from a path that does not exist would be the most misleading
    output this script could produce: every subsystem unused."""
    assert prune_evidence.main([str(tmp_path / "nope.db")]) == 1
    assert "no such database" in capsys.readouterr().err


def test_an_imported_forge_event_is_counted_but_does_not_inflate_other_subsystems(
    tmp_path,
):
    """The end-to-end version of the flag test above.

    Asserting `counts_imported is True` only proves the map is right; this proves
    the QUERY honours it, which is where the original bug lived. It also pins the
    other half: an imported row must not be credited to a subsystem that did not
    record it, or importing someone else's bundle would make this deployment look
    busier than it is.
    """
    from athena.core import db

    path = tmp_path / "forge.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('a@e.com', 'A', 'admin')"
    )
    for verb, imported in (
        ("forge_commit", "2026-01-02 00:00:00"),  # forge: imported BY DESIGN
        ("created", "2026-01-02 00:00:00"),  # someone else's history
        ("commented", None),  # local work
    ):
        conn.execute(
            "INSERT INTO activity (actor_id, verb, target_kind, target_id, "
            "created_at, imported_at) VALUES (1, ?, 'issue', 1, ?, ?)",
            (verb, "2026-01-02 00:00:00", imported),
        )
    conn.commit()
    conn.close()

    reader = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    reader.row_factory = sqlite3.Row
    try:
        by_name = {f.subsystem.name: f for f in prune_evidence.collect(reader)}
    finally:
        reader.close()

    assert by_name["forge inbound"].verb_events == 1, "imported forge event was dropped"
    assert by_name["forge inbound"].last_seen == "2026-01-02 00:00:00"
    # The imported `created` is foreign history and must NOT count; the local
    # `commented` must.
    assert by_name["issues (Aegis core)"].verb_events == 1
