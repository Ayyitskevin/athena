"""Changelog entries are per-PR fragments, assembled into CHANGELOG.md (B8).

WHY: every PR that wrote an entry edited the same `[Unreleased]` section by
hand, so parallel PRs conflicted on CHANGELOG.md. Each now adds one file under
changelog.d/; the files are validated here (so a malformed one fails the gate)
and scripts/changelog_fragments.py assembles them without rewriting or
reordering the text already in the changelog.
"""

from __future__ import annotations

import difflib
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "changelog_fragments.py"

_SPEC = importlib.util.spec_from_file_location("athena_changelog_fragments", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
fragments_script = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fragments_script
_SPEC.loader.exec_module(fragments_script)

CHANGELOG = """\
# Changelog

Intro text.

## [Unreleased]

### Security

- **An existing security entry.** Its detail
  continues here.

### Fixed

- **An existing fix.** Detail.

### Added

- **An older addition.** Detail.

### Fixed

- **A second Fixed heading's entry.** Detail.

## 0.1.0 — 2026-07-26

### Added

- **Released already.** Detail.
"""


def _write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    _write(tmp_path / "changelog.d", "README.md", "# How to write a fragment\n")
    for name, text in files.items():
        _write(tmp_path / "changelog.d", name, text)
    return tmp_path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _only_insertions(before: str, after: str) -> list[str]:
    """The lines ``after`` adds, asserting it removes or changes none."""
    added: list[str] = []
    matcher = difflib.SequenceMatcher(
        a=before.splitlines(), b=after.splitlines(), autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        assert tag in ("equal", "insert"), (tag, before.splitlines()[i1:i2])
        if tag == "insert":
            added.extend(after.splitlines()[j1:j2])
    return added


# --- the repository's own fragments ----------------------------------------------


def test_every_fragment_in_the_repository_is_valid():
    fragments, problems = fragments_script.validate(ROOT / "changelog.d")
    assert problems == []
    for fragment in fragments:
        assert fragment.category in fragments_script.CATEGORIES
    result = _run("check")
    assert result.returncode == 0, result.stderr
    assert "changelog fragments passed" in result.stdout


def test_the_fragment_directory_exists_with_its_readme():
    assert (ROOT / "changelog.d" / "README.md").is_file()
    assert "README.md" in fragments_script.IGNORED


def test_the_repository_changelog_assembles_without_losing_a_line():
    # The real CHANGELOG.md, with one synthetic fragment per category: only
    # insertions, nothing rewritten or reordered, and still one [Unreleased].
    before = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    synthetic = [
        fragments_script.Fragment(
            Path(f"probe.{category}.md"),
            category,
            f"- **Synthetic {category} entry.** Detail.\n",
        )
        for category in fragments_script.CATEGORIES
    ]
    after = fragments_script.assemble(before, synthetic)
    added = _only_insertions(before, after)
    for category in fragments_script.CATEGORIES:
        assert f"- **Synthetic {category} entry.** Detail." in added
    assert after.count("## [Unreleased]") == 1


# --- validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "mcp-tool-declarations.changed.md",
        "b8.added.md",
        "fix-42-2.fixed.md",
        "x.security.md",
        "x.deprecated.md",
        "x.removed.md",
    ],
)
def test_a_well_named_fragment_is_valid(tmp_path, name):
    _write(tmp_path, name, "- **An entry.** With detail.\n")
    fragments, problems = fragments_script.validate(tmp_path)
    assert problems == []
    assert [fragment.path.name for fragment in fragments] == [name]


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("entry.md", "name a fragment <slug>.<category>.md"),
        ("Entry.added.md", "name a fragment <slug>.<category>.md"),
        ("my_entry.added.md", "name a fragment <slug>.<category>.md"),
        ("entry.added.txt", "name a fragment <slug>.<category>.md"),
        ("entry.feature.md", "unknown category 'feature'"),
        ("entry.Added.md", "name a fragment <slug>.<category>.md"),
        ("NOTES.md", "name a fragment <slug>.<category>.md"),
    ],
)
def test_a_badly_named_fragment_is_refused(tmp_path, name, message):
    _write(tmp_path, name, "- **An entry.** With detail.\n")
    fragments, problems = fragments_script.validate(tmp_path)
    assert fragments == []
    assert len(problems) == 1
    assert problems[0].startswith(f"changelog.d/{name}: ")
    assert message in problems[0]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "is empty"),
        ("\n\n", "is empty"),
        ("- **No newline.**", "end with exactly one newline"),
        ("- **Two newlines.**\n\n", "end with exactly one newline"),
        ("**Not a bullet.**\n", "must start with one bullet"),
        ("-  \n", "must start with one bullet"),
        ("### Added\n\n- **Entry.**\n", "must start with one bullet"),
        ("- **One.**\n- **Two.**\n", "continuation lines are indented two spaces"),
        ("- **One.**\n\n### Fixed\n", "continuation lines are indented two spaces"),
        ("- **One.**\nunindented\n", "continuation lines are indented two spaces"),
        ("- **One.**\n\n\n  more\n", "two blank lines in a row"),
        ("- **Trailing.** \n", "trailing whitespace"),
        ("- **Tab.**\n\tindented\n", "a tab"),
    ],
)
def test_a_malformed_fragment_is_refused(tmp_path, text, message):
    _write(tmp_path, "entry.added.md", text)
    fragments, problems = fragments_script.validate(tmp_path)
    assert fragments == []
    assert any(message in problem for problem in problems), problems


def test_non_files_and_non_utf8_are_refused_and_a_missing_directory_is_empty(
    tmp_path,
):
    (tmp_path / "frag" / "nested.added.md").mkdir(parents=True)
    (tmp_path / "frag" / "bytes.added.md").write_bytes(b"- \xff\xfe\n")
    _, problems = fragments_script.validate(tmp_path / "frag")
    assert "changelog.d/nested.added.md: not a regular file" in problems
    assert "changelog.d/bytes.added.md: not UTF-8 text" in problems
    assert fragments_script.validate(tmp_path / "absent") == ([], [])


def test_a_multi_paragraph_fragment_is_valid(tmp_path):
    text = (
        "- **A long entry.** First paragraph\n"
        "  continues.\n"
        "\n"
        "  A second paragraph, indented.\n"
        "\n"
        "  ```bash\n"
        "  scripts/gate.sh\n"
        "  ```\n"
    )
    _write(tmp_path, "long.changed.md", text)
    fragments, problems = fragments_script.validate(tmp_path)
    assert problems == []
    assert fragments[0].text == text


# --- assembly ------------------------------------------------------------------------


def test_fragments_go_to_the_top_of_their_first_category_heading(tmp_path):
    root = _tree(
        tmp_path,
        {
            "b-second.fixed.md": "- **Second fix, by name.** Detail.\n",
            "a-first.fixed.md": "- **First fix, by name.** Detail.\n",
            "c.security.md": "- **New security entry.** Detail.\n",
        },
    )
    fragments, problems = fragments_script.validate(root / "changelog.d")
    assert problems == []
    after = fragments_script.assemble(CHANGELOG, fragments)
    assert _only_insertions(CHANGELOG, after)
    assert after == CHANGELOG.replace(
        "### Security\n\n- **An existing security entry.**",
        "### Security\n\n- **New security entry.** Detail.\n\n"
        "- **An existing security entry.**",
    ).replace(
        "### Fixed\n\n- **An existing fix.**",
        "### Fixed\n\n- **First fix, by name.** Detail.\n\n"
        "- **Second fix, by name.** Detail.\n\n- **An existing fix.**",
        1,
    )
    # The released section and the second Fixed heading are untouched.
    assert after.split("## 0.1.0")[1] == CHANGELOG.split("## 0.1.0")[1]
    assert "### Fixed\n\n- **A second Fixed heading's entry.**" in after


def test_a_category_with_no_heading_gets_one_at_the_top_in_category_order(tmp_path):
    root = _tree(
        tmp_path,
        {
            "gone.removed.md": "- **Removed a thing.** Detail.\n",
            "old.deprecated.md": "- **Deprecated a thing.** Detail.\n",
            "multi.changed.md": "- **Changed.** One\n\n  Two.\n",
        },
    )
    fragments, _ = fragments_script.validate(root / "changelog.d")
    after = fragments_script.assemble(CHANGELOG, fragments)
    assert _only_insertions(CHANGELOG, after)
    unreleased = after.split("## [Unreleased]\n\n", 1)[1]
    assert unreleased.startswith(
        "### Changed\n\n- **Changed.** One\n\n  Two.\n\n"
        "### Deprecated\n\n- **Deprecated a thing.** Detail.\n\n"
        "### Removed\n\n- **Removed a thing.** Detail.\n\n"
        "### Security\n\n- **An existing security entry.**"
    )


def test_an_empty_unreleased_section_takes_new_headings(tmp_path):
    changelog = "# Changelog\n\n## [Unreleased]\n\n## 0.1.0\n\n- **Old.**\n"
    fragment = fragments_script.Fragment(
        Path("x.added.md"), "added", "- **New.** Detail.\n"
    )
    after = fragments_script.assemble(changelog, [fragment])
    assert after == (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- **New.** Detail.\n\n"
        "## 0.1.0\n\n- **Old.**\n"
    )


def test_no_fragments_changes_nothing_and_a_missing_heading_is_refused():
    assert fragments_script.assemble(CHANGELOG, []) == CHANGELOG
    fragment = fragments_script.Fragment(Path("x.added.md"), "added", "- **A.**\n")
    with pytest.raises(ValueError, match=r"exactly one '## \[Unreleased\]'"):
        fragments_script.assemble("# Changelog\n\n## 0.1.0\n", [fragment])
    with pytest.raises(ValueError, match="exactly one"):
        fragments_script.assemble(CHANGELOG + "\n## [Unreleased]\n", [fragment])


def test_dry_run_prints_the_change_and_writes_nothing(tmp_path):
    root = _tree(tmp_path, {"probe.fixed.md": "- **A probe fix.** Detail.\n"})
    result = _run("--root", str(root), "assemble", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "+- **A probe fix.** Detail." in result.stdout
    assert "--- a/CHANGELOG.md" in result.stdout
    assert "would delete changelog.d/probe.fixed.md" in result.stdout
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG
    assert (root / "changelog.d" / "probe.fixed.md").is_file()


def test_assemble_writes_the_changelog_and_deletes_only_the_fragments(tmp_path):
    root = _tree(tmp_path, {"probe.fixed.md": "- **A probe fix.** Detail.\n"})
    result = _run("--root", str(root), "assemble")
    assert result.returncode == 0, result.stderr
    assert "assembled and deleted changelog.d/probe.fixed.md" in result.stdout
    after = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert after == CHANGELOG.replace(
        "### Fixed\n\n- **An existing fix.**",
        "### Fixed\n\n- **A probe fix.** Detail.\n\n- **An existing fix.**",
        1,
    )
    assert sorted(p.name for p in (root / "changelog.d").iterdir()) == ["README.md"]
    # A second run has nothing to do.
    again = _run("--root", str(root), "assemble")
    assert again.returncode == 0
    assert "no changelog fragments to assemble" in again.stdout
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == after


def test_one_invalid_fragment_stops_assembly_and_check(tmp_path):
    root = _tree(
        tmp_path,
        {
            "good.added.md": "- **Fine.** Detail.\n",
            "bad.feature.md": "- **Wrong category.**\n",
        },
    )
    for args in (("check",), ("assemble",), ("assemble", "--dry-run")):
        result = _run("--root", str(root), *args)
        assert result.returncode == 1, args
        assert "changelog fragments failed: 1 problem" in result.stderr
        assert "changelog.d/bad.feature.md: unknown category" in result.stderr
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG
    assert (root / "changelog.d" / "good.added.md").is_file()


def test_assemble_refuses_a_changelog_without_unreleased(tmp_path):
    root = _tree(tmp_path, {"probe.added.md": "- **A.** Detail.\n"})
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.0\n", encoding="utf-8")
    result = _run("--root", str(root), "assemble")
    assert result.returncode == 1
    assert "exactly one '## [Unreleased]' heading" in result.stderr
    assert (root / "changelog.d" / "probe.added.md").is_file()
