"""The work order: six parts an issue body may carry as headings (W3, D21).

A seat should be able to tell, before it claims, whether an issue says what
done means. The issue template gives the body six headings (goal, context,
inputs, action, format, success criteria), the parts the build brief in
``docs/plans/BUILD_BRIEF_TEMPLATE.md`` uses. The work-context packet reports
which parts a body has and which it lacks, through :func:`report`.

The parts are headings, not columns (D21): nothing is stored, the body stays
the free text it always was, and a missing part refuses nothing. The rule is
deliberately mechanical so every reader gets the same answer from the same
bytes; no model and no fuzzy matching:

1. Lines split at ``\\n``, ``\\r\\n`` or ``\\r``.
2. A fence opens at a line that starts (after at most three spaces) with three
   or more backticks or tildes; a backtick fence's line holds no other
   backtick. It closes at a line of at least as many of the same character and
   nothing else. An unclosed fence runs to the end of the body. No line inside
   a fence, or a fence's own lines, is a heading.
3. A heading is an ATX heading: at most three spaces, one to six ``#``, then a
   space, a tab or the end of the line. Its text loses an optional closing run
   of ``#``, surrounding space and one trailing ``:``; inner whitespace folds
   to one space and case folds. Every level counts. Underlined (setext)
   headings, quoted headings and headings with emphasis or numbering do not.
4. The text names a part only by exact match against :data:`NAMES`, a closed
   list: the part's own name, the singular or plural of inputs and action, and
   ``acceptance criteria`` for success criteria.
5. A part is present when one of its headings has at least one line that is
   not blank and not a heading before the next heading of the same or a
   higher level (fewer or as many ``#``). Lines inside a fence count as text.
   A heading with nothing under it, as the template leaves each part, is
   missing.

The report is computed over the whole body, not the packet's bounded excerpt,
and names parts only: no heading or body text is echoed.
"""

from __future__ import annotations

import re

#: The six parts, in the order the template writes them and the report lists them.
PARTS = ("goal", "context", "inputs", "action", "format", "success_criteria")

#: Every heading text that names a part, after normalisation: a closed list.
NAMES: dict[str, str] = {
    "goal": "goal",
    "context": "context",
    "inputs": "inputs",
    "input": "inputs",
    "action": "action",
    "actions": "action",
    "format": "format",
    "success criteria": "success_criteria",
    "acceptance criteria": "success_criteria",
}

#: The template's name: the new-issue form starts from it at
#: ``/aegis/issues/new?template=work-order``.
TEMPLATE_NAME = "work-order"

#: The issue template: the six parts as level-two headings, each left empty so
#: an unfilled template reports every part missing.
TEMPLATE = (
    "## Goal\n\n"
    "## Context\n\n"
    "## Inputs\n\n"
    "## Action\n\n"
    "## Format\n\n"
    "## Success criteria\n"
)

#: The rule in one paragraph, for the packet's and the tools' descriptions.
RULE = (
    "Which of the six work-order parts (goal, context, inputs, action, format, "
    "success_criteria) the issue body has as Markdown headings, and which it "
    "lacks; nothing is refused for a missing part. A part is a # to ###### "
    "heading (at most three spaces before it) whose text, without a closing #, "
    "a trailing colon and case, is its name, or Input, Actions or Acceptance "
    "criteria; it is present when a line that is not blank and not a heading "
    "follows before the next heading of the same or a higher level. Headings in "
    "fenced code do not count. Computed over the whole body; names parts only."
)

_LINES = re.compile(r"\r\n|\r|\n")
_SPACE = re.compile(r"\s+")

# The body is text another account wrote, up to 200,000 characters, and this
# runs on every work-context read. Fences and headings are therefore recognised
# with plain string operations, each linear in the line: a pattern with a lazy
# or nested repeat here takes time quadratic in a line of spaces or backticks.


def _indent(line: str) -> tuple[int, str]:
    rest = line.lstrip(" ")
    return len(line) - len(rest), rest


def _fence_opening(line: str) -> str | None:
    """The fence a line opens (its run of backticks or tildes), or None."""
    indent, rest = _indent(line)
    if indent > 3:
        return None
    for mark in ("`", "~"):
        if rest.startswith(mark * 3):
            run = len(rest) - len(rest.lstrip(mark))
            if mark == "`" and "`" in rest[run:]:
                return None  # a backtick fence's line holds no other backtick
            return mark * run
    return None


def _heading(line: str) -> tuple[int, str] | None:
    """An ATX heading's level and text, or None."""
    indent, rest = _indent(line)
    if indent > 3:
        return None
    level = len(rest) - len(rest.lstrip("#"))
    if not 1 <= level <= 6:
        return None
    text = rest[level:]
    if text and text[0] not in " \t":
        return None
    return level, text.strip(" \t")


def _part(text: str) -> str | None:
    """The part a heading's text names, or None."""
    unclosed = text.rstrip("#")
    if unclosed != text and (not unclosed or unclosed[-1] in " \t"):
        text = unclosed  # an optional closing run of #, set off by a space
    text = text.strip()
    if text.endswith(":"):
        text = text[:-1]
    return NAMES.get(_SPACE.sub(" ", text).strip().casefold())


def report(body: str | None) -> dict[str, list[str]]:
    """``{"present": [...], "missing": [...]}``: the parts by the module's rule,
    each list in :data:`PARTS` order, together naming every part once."""
    present: set[str] = set()
    # Open sections: (level, part or None). A heading closes every open section
    # of its level or deeper; a text line fills every open part.
    open_sections: list[tuple[int, str | None]] = []
    fence: str | None = None
    for line in _LINES.split(body or ""):
        if fence is not None:
            stripped = line.strip()
            if (
                len(line) - len(line.lstrip(" ")) <= 3
                and stripped
                and set(stripped) == {fence[0]}
                and len(stripped) >= len(fence)
            ):
                fence = None
            present.update(part for _, part in open_sections if part)
            continue
        opened = _fence_opening(line)
        if opened is not None:
            fence = opened
            present.update(part for _, part in open_sections if part)
            continue
        heading = _heading(line)
        if heading is not None:
            level, text = heading
            while open_sections and open_sections[-1][0] >= level:
                open_sections.pop()
            open_sections.append((level, _part(text)))
            continue
        if line.strip():
            present.update(part for _, part in open_sections if part)
    return {
        "present": [part for part in PARTS if part in present],
        "missing": [part for part in PARTS if part not in present],
    }
