"""AGENTS.md describes the tree as it is, and its change recipes name real files.

WHY (B4): AGENTS.md is re-read every session, so a contributor trusts its map.
Its module table had silently omitted ``mcp/`` (140 tools) and ``field_guide/``,
and its orientation tree ``workflows/``; and nothing said which files a new
tool, route, migration or setting touches, so each contributor rediscovered it.
These tests fail when a directory under ``src/athena/`` is added without a row,
and when a recipe names a path, script or test file that does not exist.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
SOURCE = ROOT / "src" / "athena"

# The kinds of change the recipe table must cover, by the start of its first cell.
REQUIRED_RECIPES = (
    "MCP tool",
    "Agent-facing REST route",
    "Browser route",
    "Refusal code",
    "Write command",
    "Migration",
    "Setting",
    "Activity reader",
    "Free text in an agent packet",
    "Entry document",
    "Changelog entry",
    "Test",
)
CHANGED_PROFILE = "scripts/gate.sh --profile changed"
_CODE = re.compile(r"`([^`]+)`")
_FILE_SUFFIXES = (".py", ".md", ".toml", ".sql", ".sh", ".json", ".html")


def _section(heading: str) -> str:
    text = AGENTS.read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _table_rows(section: str) -> list[list[str]]:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("|")
    ]
    return rows[2:]  # drop the header and its rule


def _directories() -> set[str]:
    return {
        path.name
        for path in SOURCE.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    }


def test_the_module_table_lists_every_directory_under_src_athena():
    rows = _table_rows(_section("Module ownership"))
    listed = {
        match.group(1)
        for row in rows
        for match in re.finditer(r"`src/athena/([a-z_]+)/`", row[1])
    }
    missing = sorted(_directories() - listed)
    assert not missing, (
        f"src/athena/ has directories AGENTS.md's module table omits: {missing}. "
        "Add a row (area, path, one-line role) for each."
    )
    stale = sorted(listed - _directories())
    assert not stale, f"the module table lists directories that do not exist: {stale}"


def test_the_orientation_tree_lists_every_directory_and_top_level_module():
    section = _section("Quick orientation")
    tree = section[
        section.index("```") : section.index("```", section.index("```") + 3)
    ]
    entries = set(re.findall(r"^  ([a-z_]+/|[a-z_]+\.py)\s", tree, re.MULTILINE))
    expected = {f"{name}/" for name in _directories()} | {
        path.name for path in SOURCE.glob("*.py") if path.name != "__init__.py"
    }
    missing = sorted(expected - entries)
    assert not missing, f"AGENTS.md's orientation tree omits: {missing}"
    stale = sorted(entries - expected)
    assert not stale, f"AGENTS.md's orientation tree lists what does not exist: {stale}"


def _recipes() -> dict[str, list[str]]:
    section = _section("Change recipes")
    header = next(line for line in section.splitlines() if line.startswith("|"))
    assert CHANGED_PROFILE in header, (
        "the recipe table's test column must say every row ends with "
        f"`{CHANGED_PROFILE}`"
    )
    return {row[0]: row for row in _table_rows(section)}


def test_the_recipe_table_covers_every_kind_of_change():
    recipes = _recipes()
    missing = [kind for kind in REQUIRED_RECIPES if kind not in recipes]
    assert not missing, f"AGENTS.md's change recipes lack rows for: {missing}"
    for kind, row in recipes.items():
        assert len(row) == 4, f"recipe {kind!r} needs: change, edit, generate, tests"
        assert re.search(r"`tests/test_[a-z0-9_]+\.py`", row[3]), (
            f"recipe {kind!r} names no test file to run"
        )


def _paths(cell: str) -> list[str]:
    """Every code span in ``cell`` that names a file or directory of the tree."""
    named = []
    for span in _CODE.findall(cell):
        token = span.split()[0]
        if "/" in token or token.endswith(_FILE_SUFFIXES):
            named.append(token)
    return named


def _exists(token: str) -> bool:
    # A placeholder (<area>, NNNN) stands for any name: the pattern must match.
    pattern = re.sub(r"<[^>]+>", "*", token).replace("NNNN", "[0-9]" * 4)
    if "*" in pattern or "[" in pattern:
        return any(ROOT.glob(pattern.rstrip("/")))
    return (ROOT / token).exists()


@pytest.mark.parametrize("kind", REQUIRED_RECIPES)
def test_every_path_a_recipe_names_exists(kind):
    row = _recipes()[kind]
    named = [token for cell in row[1:] for token in _paths(cell)]
    assert named, f"recipe {kind!r} names no file"
    missing = [token for token in named if not _exists(token)]
    assert not missing, (
        f"recipe {kind!r} names paths that do not exist: {missing}. "
        "A recipe must describe today's tree."
    )


def test_the_path_check_refuses_what_is_not_there():
    assert _exists("scripts/generate_agent_api.py")
    assert _exists("src/athena/mcp/tools_<area>.py")
    assert _exists("src/athena/core/migrations/NNNN_<name>.sql")
    assert not _exists("scripts/generate_everything.py")
    assert not _exists("src/athena/mcp/widgets_<area>.py")
    assert _paths("`tests/test_x.py` (`commit=`), `@tool`, `a/b/` `x.md --check`") == [
        "tests/test_x.py",
        "a/b/",
        "x.md",
    ]
