"""COMMAND_MIGRATION.md's debtor tables are generated — this is the drift gate.

WHY (B6a): the migration inventory was the review-facing source of truth and was
kept by hand. It said "None known" for sprints and API tokens while every one of
their commands still took a bare ``actor_id`` instead of the resolved actor —
the first rule of a completed slice. The two kinds of debt the code states
mechanically are now generated (``scripts/generate_command_debtors.py``); this
test fails when the committed tables no longer match the code, and when the
hand-written column claims no debt where the generated table finds some.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "COMMAND_MIGRATION.md"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_command_debtors", REPO / "scripts" / "generate_command_debtors.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_command_debtors"] = module
    spec.loader.exec_module(module)
    return module


def test_command_debtor_tables_match_the_code():
    generator = _load_generator()
    assert DOC.read_text() == generator.render(), (
        "docs/COMMAND_MIGRATION.md's debtor tables are stale — regenerate with "
        "`python scripts/generate_command_debtors.py`"
    )


def test_the_bare_actor_rule_finds_known_debtors_and_spares_migrated_commands():
    """Canaries on both sides of the rule, so an AST pass that matches nothing
    (or everything) fails here rather than rendering a well-formed lie."""
    debtors = _load_generator().bare_actor_commands()
    assert "mint_token" in debtors["athena.core.token_commands"]
    assert "create_project" in debtors["athena.aegis.project_commands"]
    # Migrated: these take the resolved actor and own their gate.
    assert "athena.mentor.page_commands" not in debtors
    assert "athena.aegis.sprint_commands" not in debtors  # paid in G1
    assert "athena.aegis.comment_commands" in debtors  # only its automation entry
    assert debtors["athena.aegis.comment_commands"] == ["create_comment_as_automation"]
    assert "set_blocked_close_policy" not in debtors["athena.aegis.project_commands"]


def test_the_bare_actor_rule_matches_an_independent_reading():
    """The rule, restated: a public command whose parameters name ``actor_id``
    and not ``actor``. Recomputed here from the signatures by import, so the
    generator's AST reading and the live functions cannot disagree."""
    import inspect

    debtors = _load_generator().bare_actor_commands()
    for module_name, names in debtors.items():
        module = importlib.import_module(module_name)
        for name in names:
            params = inspect.signature(getattr(module, name)).parameters
            assert "actor_id" in params and "actor" not in params, (module_name, name)
    for path in sorted((REPO / "src" / "athena").rglob("*_commands.py")):
        module_name = "athena." + ".".join(
            path.relative_to(REPO / "src" / "athena").with_suffix("").parts
        )
        defined = {
            node.name
            for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        }
        module = importlib.import_module(module_name)
        for name in defined:
            params = inspect.signature(getattr(module, name)).parameters
            listed = name in debtors.get(module_name, [])
            assert listed == ("actor_id" in params and "actor" not in params), (
                module_name,
                name,
            )


def test_no_hand_written_row_claims_none_known_for_a_listed_module():
    """A hand-written "None known" must not stand beside generated debt.

    The inventory's rows name areas, not modules, so the check is on the row's
    own words: a row whose debt cell begins "None" and belongs to an area whose
    command module the generated table lists must point at that table. The
    areas are matched by the module's stem (``sprint_commands`` → "Sprints")."""
    debtors = _load_generator().bare_actor_commands()
    stems = {name.rsplit(".", 1)[1].removesuffix("_commands") for name in debtors}
    rows = [
        line
        for line in DOC.read_text()
        .split("## Current inventory", 1)[1]
        .split("\n## ", 1)[0]
        .splitlines()
        if line.startswith("| ") and not line.startswith("| Area") and "---" not in line
    ]
    assert rows, "the inventory table was not found"
    contradicted = []
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        area, debt = cells[0].lower(), cells[-1]
        if not debt.startswith("None") or "(#generated-debtors)" in debt:
            continue
        if any(stem in area or area.rstrip("s") in stem for stem in stems):
            contradicted.append(cells[0])
    assert contradicted == []


def test_outside_command_table_is_the_ownership_checks_allowlist():
    generator = _load_generator()
    writes = generator.outside_command_writes()
    rendered = generator.render_outside_command()
    for _, module, function in writes:
        assert f"`{module}.{function}`" in rendered
    assert f"*{len(writes)} writers.*" in rendered
