"""Generated blocks inside hand-written Markdown documents.

A document that is mostly prose can still carry an inventory that must not
drift: the generator owns the lines between a pair of markers and nothing else,
so the prose around them stays hand-written. A block looks like::

    <!-- BEGIN GENERATED settings:server (scripts/generate_settings_doc.py; do not edit by hand) -->
    ...generated lines...
    <!-- END GENERATED settings:server -->

``splice`` replaces the lines of every named block and refuses a document whose
blocks do not match the names the generator renders, so a block cannot be
deleted, duplicated or orphaned without the ``--check`` run failing.
Standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import sys

_BLOCK = re.compile(
    r"^<!-- BEGIN GENERATED (?P<name>[\w:.+-]+) \((?P<source>[^)]*)\) -->\n"
    r"(?P<body>.*?)"
    r"^<!-- END GENERATED (?P=name) -->$",
    re.MULTILINE | re.DOTALL,
)
_BEGIN = re.compile(r"^<!-- BEGIN GENERATED (?P<name>[\w:.+-]+) ", re.MULTILINE)


def begin_marker(name: str, source: str) -> str:
    return f"<!-- BEGIN GENERATED {name} ({source}; do not edit by hand) -->"


def end_marker(name: str) -> str:
    return f"<!-- END GENERATED {name} -->"


def block_names(text: str) -> list[str]:
    """Every block opened in ``text``, in document order (duplicates kept)."""
    return [match.group("name") for match in _BEGIN.finditer(text)]


def splice(text: str, blocks: Mapping[str, str], *, prefix: str, source: str) -> str:
    """``text`` with each block's lines replaced by ``blocks[name]``.

    Every block whose name starts with ``prefix`` must be in ``blocks``, and
    every entry of ``blocks`` must appear exactly once, closed by its own end
    marker; anything else raises ``ValueError`` naming the problem."""
    opened = [name for name in block_names(text) if name.startswith(prefix)]
    duplicated = sorted({name for name in opened if opened.count(name) > 1})
    if duplicated:
        raise ValueError(f"generated blocks opened more than once: {duplicated}")
    missing = sorted(set(blocks) - set(opened))
    if missing:
        raise ValueError(f"the document has no generated block for: {missing}")
    unknown = sorted(set(opened) - set(blocks))
    if unknown:
        raise ValueError(f"generated blocks no generator renders: {unknown}")

    closed: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in blocks:
            return match.group(0)
        closed.add(name)
        body = blocks[name].rstrip("\n") + "\n"
        return f"{begin_marker(name, source)}\n{body}{end_marker(name)}"

    result = _BLOCK.sub(replace, text)
    unclosed = sorted(set(blocks) - closed)
    if unclosed:
        raise ValueError(f"generated blocks without a matching end marker: {unclosed}")
    return result


def outside_blocks(text: str) -> str:
    """``text`` with every generated block's lines removed (markers included)."""
    return _BLOCK.sub("", text)


def write_or_check(
    doc: Path, content: str, *, repo: Path, command: str, check: bool | None = None
) -> int:
    """The shared ``main`` tail: rewrite ``doc``, or with ``--check`` report drift.
    ``check`` overrides reading ``--check`` from the command line."""
    relative = doc.relative_to(repo)
    if check is None:
        check = "--check" in sys.argv[1:]
    if check:
        if doc.read_text() != content:
            sys.stderr.write(f"{relative} is stale — regenerate with `{command}`\n")
            return 1
        print(f"{relative} matches the code")
        return 0
    doc.write_text(content)
    print(f"wrote {relative}")
    return 0
