"""Validate changelog fragments, and assemble them into CHANGELOG.md.

Every pull request with a notable change adds one small Markdown file to
``changelog.d/`` instead of editing CHANGELOG.md's ``[Unreleased]`` section,
which parallel pull requests used to conflict on. A fragment is named

    changelog.d/<slug>.<category>.md

where ``<slug>`` is lowercase letters, digits and hyphens (use your branch's
topic, adding ``-2`` and so on for a second entry) and ``<category>`` is a Keep a
Changelog category: added, changed, deprecated, removed, fixed or security. Its
text is the entry exactly as it will read in CHANGELOG.md: one bullet, starting
``- `` (house style: ``- **A bold sentence.**`` and then the detail), with every
further line, and every further paragraph, indented by two spaces.
``changelog.d/README.md`` explains this and is ignored.

Usage:
    python scripts/changelog_fragments.py check              # validate fragments
    python scripts/changelog_fragments.py assemble --dry-run # print the change
    python scripts/changelog_fragments.py assemble           # write it, and
                                                             # delete the fragments

Assembly puts each category's fragments, in file-name order, at the top of the
first ``### <Category>`` heading under ``## [Unreleased]``, or under a new
heading at the top of the section when there is none. It never rewrites or
reorders the text already there. ``tests/test_changelog_fragments.py`` runs the
check, so an invalid fragment fails the gate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
from pathlib import Path
import re
import sys
from typing import Iterable

REPO = Path(__file__).resolve().parent.parent
FRAGMENT_DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
IGNORED = frozenset({"README.md"})
UNRELEASED = "## [Unreleased]"
# Keep a Changelog's categories, in the order a new heading is placed.
CATEGORIES = ("added", "changed", "deprecated", "removed", "fixed", "security")
NAME_RE = re.compile(r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.(?P<category>[a-z]+)\.md$")


@dataclass(frozen=True)
class Fragment:
    path: Path
    category: str
    text: str  # the entry, ending in exactly one newline

    @property
    def heading(self) -> str:
        return f"### {self.category.capitalize()}"


def _content_problems(text: str) -> list[str]:
    """What is wrong with a fragment's text, if anything."""
    if not text.strip():
        return ["is empty"]
    problems: list[str] = []
    if not text.endswith("\n") or text.endswith("\n\n"):
        problems.append("must end with exactly one newline")
    lines = text.rstrip("\n").split("\n")
    if not lines[0].startswith("- ") or not lines[0][2:].strip():
        problems.append("must start with one bullet, '- ' and the entry")
    for number, line in enumerate(lines[1:], start=2):
        if line == "":
            if lines[number - 2] == "":
                problems.append(f"line {number}: two blank lines in a row")
            continue
        if not line.startswith("  "):
            problems.append(
                f"line {number}: continuation lines are indented two spaces "
                "(a fragment is one bullet; a heading or second bullet is not "
                "allowed)"
            )
    for number, line in enumerate(lines, start=1):
        if line != line.rstrip():
            problems.append(f"line {number}: trailing whitespace")
        if "\t" in line:
            problems.append(f"line {number}: a tab; indent with spaces")
    return problems


def validate(directory: Path) -> tuple[list[Fragment], list[str]]:
    """Every fragment in ``directory`` in file-name order, and every problem
    found (``path: problem``). A missing directory holds no fragments."""
    fragments: list[Fragment] = []
    problems: list[str] = []
    if not directory.is_dir():
        return fragments, problems
    for path in sorted(directory.iterdir()):
        if path.name in IGNORED:
            continue
        where = f"{FRAGMENT_DIR}/{path.name}"
        match = NAME_RE.match(path.name)
        if not path.is_file() or path.is_symlink():
            problems.append(f"{where}: not a regular file")
            continue
        if match is None:
            problems.append(
                f"{where}: name a fragment <slug>.<category>.md, with a slug of "
                "lowercase letters, digits and hyphens"
            )
            continue
        if match["category"] not in CATEGORIES:
            problems.append(
                f"{where}: unknown category {match['category']!r}; use one of "
                + ", ".join(CATEGORIES)
            )
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{where}: not UTF-8 text")
            continue
        found = _content_problems(text)
        if found:
            problems.extend(f"{where}: {problem}" for problem in found)
            continue
        fragments.append(Fragment(path, match["category"], text))
    return fragments, problems


def assemble(changelog: str, fragments: Iterable[Fragment]) -> str:
    """CHANGELOG.md's text with the fragments written into ``[Unreleased]``."""
    lines = changelog.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == UNRELEASED]
    if len(starts) != 1:
        raise ValueError(f"{CHANGELOG} must have exactly one '{UNRELEASED}' heading")
    start = starts[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    by_category: dict[str, list[str]] = {}
    for fragment in fragments:
        by_category.setdefault(fragment.category, []).append(fragment.text)
    if not by_category:
        return changelog

    insertions: dict[int, list[str]] = {}  # line index -> lines to insert before
    new_headings: list[str] = []
    for category in CATEGORIES:
        entries = by_category.get(category)
        if not entries:
            continue
        block = "\n".join(entries)
        heading = f"### {category.capitalize()}"
        existing = next(
            (i for i in range(start + 1, end) if lines[i].rstrip("\n") == heading),
            None,
        )
        if existing is None:
            new_headings.append(f"{heading}\n\n{block}\n")
            continue
        # The first non-blank line after the heading: the newest entry so far.
        first = next((i for i in range(existing + 1, end) if lines[i].strip()), end)
        insertions.setdefault(first, []).append(f"{block}\n")
    if new_headings:
        # After the [Unreleased] heading and the blank line under it.
        top = start + 1
        while top < end and not lines[top].strip():
            top += 1
        insertions.setdefault(top, []).insert(0, "".join(new_headings))
    out: list[str] = []
    for index in range(len(lines) + 1):
        out.extend(insertions.get(index, []))
        if index < len(lines):
            out.append(lines[index])
    return "".join(out)


def _report(problems: list[str]) -> int:
    print(
        f"changelog fragments failed: {len(problems)} "
        f"{'problem' if len(problems) == 1 else 'problems'}",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="validate changelog.d/ fragments, or assemble them into "
        "CHANGELOG.md's [Unreleased] section"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO,
        help="the directory holding CHANGELOG.md and changelog.d/ (default: "
        "this repository)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate every fragment")
    assembling = commands.add_parser(
        "assemble", help="write the fragments into CHANGELOG.md and delete them"
    )
    assembling.add_argument(
        "--dry-run",
        action="store_true",
        help="print the change to CHANGELOG.md and the fragments it would "
        "delete; write nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = args.root.absolute()

    fragments, problems = validate(root / FRAGMENT_DIR)
    if problems:
        return _report(problems)
    if args.command == "check":
        print(f"changelog fragments passed: {len(fragments)} valid")
        return 0

    changelog_path = root / CHANGELOG
    before = changelog_path.read_text(encoding="utf-8")
    try:
        after = assemble(before, fragments)
    except ValueError as exc:
        return _report([str(exc)])
    if not fragments:
        print("no changelog fragments to assemble")
        return 0
    if args.dry_run:
        sys.stdout.writelines(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{CHANGELOG}",
                tofile=f"b/{CHANGELOG}",
            )
        )
        for fragment in fragments:
            print(f"would delete {FRAGMENT_DIR}/{fragment.path.name}")
        return 0
    changelog_path.write_text(after, encoding="utf-8")
    for fragment in fragments:
        fragment.path.unlink()
        print(f"assembled and deleted {FRAGMENT_DIR}/{fragment.path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
