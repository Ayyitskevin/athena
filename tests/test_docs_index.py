"""docs/INDEX.md reaches every document under docs/, and its links resolve.

WHY (B6a): the index is the map an agent or a new contributor reads first, and
it was kept by hand. It had silently dropped five documents (the glossary and
the closeout recipe among them) and the whole decision-record directory, so the
documents existed but nothing pointed at them. This test makes the map complete
by construction: a new document fails here until it has a row.

A document is reachable when INDEX.md links it, or when it sits inside a
directory INDEX.md links under one of the rules below:

- ``readme``: the directory's README.md is its index, and links every other
  file in it (``decisions/``: each record is listed with its date and status);
- ``whole``: the row covers the directory as a unit and says so ("listed as a
  directory, not file by file") — working plans and their reports, research
  notes, fixtures, images.

Every relative link in INDEX.md and in each ``readme`` index must resolve to an
existing file (a directory row to an existing directory).
"""

from __future__ import annotations

from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
INDEX = DOCS / "INDEX.md"

DIRECTORY_RULES = {
    "decisions": "readme",
    "plans": "whole",
    "research": "whole",
    "evidence-convention": "whole",
    "assets": "whole",
}
# The few files under docs/ that need no row, each with its reason.
EXCLUDED = {
    "INDEX.md": "the index itself",
}
_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)#\s]+)(?:#[^)]*)?\)")


def _links(path: Path) -> list[str]:
    return _LINK.findall(path.read_text())


def _resolved(source: Path, target: str) -> Path:
    return (source.parent / target).resolve()


def _documents() -> list[Path]:
    return sorted(
        path
        for path in DOCS.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def test_every_document_under_docs_is_reachable_from_the_index():
    linked = {_resolved(INDEX, target) for target in _links(INDEX)}
    unreachable = []
    for path in _documents():
        relative = path.relative_to(DOCS)
        if str(relative) in EXCLUDED or path.resolve() in linked:
            continue
        top = relative.parts[0] if len(relative.parts) > 1 else None
        rule = DIRECTORY_RULES.get(top) if top is not None else None
        if rule == "whole":
            continue
        if rule == "readme":
            readme = DOCS / top / "README.md"
            indexed = {_resolved(readme, target) for target in _links(readme)}
            if path == readme or path.resolve() in indexed:
                continue
        unreachable.append(str(relative))
    assert unreachable == [], (
        "these documents are not reachable from docs/INDEX.md — add a row in its "
        f"existing style, or a rule in this test with its reason: {unreachable}"
    )


def test_every_directory_rule_is_a_row_of_the_index():
    """A rule that INDEX.md does not state is a hidden exclusion."""
    text = INDEX.read_text()
    for directory, rule in DIRECTORY_RULES.items():
        assert (DOCS / directory).is_dir(), f"rule for a missing directory: {directory}"
        assert f"]({directory}/)" in text, f"INDEX.md does not list {directory}/"
        row = next(line for line in text.splitlines() if f"]({directory}/)" in line)
        if rule == "whole":
            assert "listed as a directory, not file by file" in row, row
        else:
            assert f"]({directory}/README.md)" in row, row
    for path in DOCS.iterdir():
        if path.is_dir() and path.name != "__pycache__":
            assert path.name in DIRECTORY_RULES, (
                f"docs/{path.name}/ has no rule: give it a row in INDEX.md and an "
                "entry in DIRECTORY_RULES"
            )
    for name in EXCLUDED:
        assert (DOCS / name).exists(), f"stale exclusion: {name}"


def test_every_relative_link_in_the_indexes_resolves():
    sources = [INDEX] + [
        DOCS / directory / "README.md"
        for directory, rule in DIRECTORY_RULES.items()
        if rule == "readme"
    ]
    broken = []
    for source in sources:
        links = _links(source)
        assert links, f"{source.name} has no relative links; the extractor broke"
        for target in links:
            resolved = _resolved(source, target)
            if target.endswith("/"):
                ok = resolved.is_dir()
            else:
                ok = resolved.is_file()
            if not ok:
                broken.append(f"{source.relative_to(REPO)} -> {target}")
    assert broken == []


def test_the_link_extractor_sees_what_it_must():
    """Canaries: rows the index has carried since it was written, and one of the
    rows that went missing."""
    targets = set(_links(INDEX))
    assert {"VISION.md", "../AGENTS.md", "plans/", "GLOSSARY.md"} <= targets
