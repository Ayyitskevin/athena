"""Every word an agent meets in a tool name is defined in the glossary.

A tool name is the first thing an agent reads about a capability, and names
like `my_desk`, `my_office`, `get_project_floor`, `yield_claim` and
`claim_next_ready` use words that mean something exact in Athena and something
looser everywhere else. The glossary defined none of them (A14). So: split every
MCP tool name (the full set, which every profile is a subset of) on
underscores, drop the plain verbs and connectives listed below, and require a
glossary row for each word left, with a filled "Not the same as" cell, since
the word's ordinary meaning is the one most likely to mislead.

A row names its words in its Term cell, each in bold (`**Claim**`,
`**Archive** / **unarchive**`); a plural tool word (`issues`) is covered by
its singular's row.
"""

from __future__ import annotations

from pathlib import Path
import re

from athena.mcp import server as mcp_server

GLOSSARY = Path(__file__).resolve().parents[1] / "docs" / "GLOSSARY.md"

# Words a tool name may use without a glossary row. Only a verb or a connective
# whose meaning in a tool name is the plain English one goes here: it acts on
# the noun after it and names no Athena act with rules of its own. A verb that
# does name such an act (claim, yield, watch, delegate, archive, complete,
# resume, ...) gets a row instead, and a noun never goes here to make this test
# pass. Every entry must still appear in some tool name (a test holds that), so
# the list cannot collect words nobody uses.
VERBS_AND_CONNECTIVES = frozenset(
    {
        # verbs
        "attach",
        "begin",
        "cancel",
        "clear",
        "count",
        "create",
        "decide",
        "delete",
        "detach",
        "find",
        "get",
        "list",
        "mark",
        "open",
        "read",
        "record",
        "request",
        "resolve",
        "restore",
        "set",
        "start",
        "update",
        # connectives
        "by",
        "for",
        "my",
        "on",
        "to",
    }
)

# The words the requirement named as undefined. `inbox` is in no tool name but
# in the descriptions of three tools that each mean a different inbox.
NAMED_BY_A14 = ("desk", "office", "floor", "inbox", "watch", "yield", "claim-next")

_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def glossary_rows(text: str) -> list[dict[str, str]]:
    """Every row of every table in the text, keyed by its table's header."""
    lines = text.splitlines()
    rows: list[dict[str, str]] = []
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("|") or not re.match(r"^\|\s*-", lines[index + 1]):
            continue
        header = _cells(line)
        for body in lines[index + 2 :]:
            if not body.startswith("|"):
                break
            rows.append(dict(zip(header, _cells(body), strict=False)))
    return rows


def terms(row: dict[str, str]) -> set[str]:
    return {term.strip().lower() for term in _BOLD.findall(row.get("Term", ""))}


def covering_row(word: str, rows: list[dict[str, str]]) -> dict[str, str] | None:
    """The row that defines ``word``, or its singular for a plural."""
    candidates = {word}
    if word.endswith("s"):
        candidates.add(word[:-1])
    if word.endswith("es"):
        candidates.add(word[:-2])
    for row in rows:
        if terms(row) & candidates:
            return row
    return None


def tool_words() -> dict[str, list[str]]:
    """Each word of every tool name, with the tools that use it."""
    words: dict[str, list[str]] = {}
    for name in mcp_server.declared():
        for word in name.split("_"):
            words.setdefault(word, []).append(name)
    return words


def undefined(words: dict[str, list[str]], rows: list[dict[str, str]]) -> list[str]:
    return sorted(
        f"{word} (in {', '.join(tools[:3])})"
        for word, tools in words.items()
        if word not in VERBS_AND_CONNECTIVES and covering_row(word, rows) is None
    )


def _rows() -> list[dict[str, str]]:
    return glossary_rows(GLOSSARY.read_text(encoding="utf-8"))


def test_every_glossary_table_has_a_not_the_same_as_column():
    text = GLOSSARY.read_text(encoding="utf-8")
    rows = glossary_rows(text)
    assert rows, "docs/GLOSSARY.md has no table rows"
    assert all("Term" in row and "Not the same as" in row for row in rows), (
        "every glossary table needs a Term and a 'Not the same as' column"
    )


def test_every_word_in_a_tool_name_has_a_glossary_row():
    missing = undefined(tool_words(), _rows())
    assert missing == [], (
        "these words appear in MCP tool names and have no row in "
        f"docs/GLOSSARY.md: {missing}. Add a row that defines the word as the "
        "code uses it; put a word on VERBS_AND_CONNECTIVES only if it is a plain "
        "verb or connective"
    )


def test_every_tool_word_row_says_what_the_word_is_not():
    rows = _rows()
    empty = sorted(
        word
        for word in tool_words()
        if word not in VERBS_AND_CONNECTIVES
        and (row := covering_row(word, rows)) is not None
        and not row.get("Not the same as", "").strip(" -—")
    )
    assert empty == [], f"glossary rows with no 'Not the same as' text: {empty}"


def test_the_words_a14_named_have_rows():
    rows = _rows()
    missing = [word for word in NAMED_BY_A14 if covering_row(word, rows) is None]
    assert missing == [], f"docs/GLOSSARY.md still does not define: {missing}"


def test_every_listed_verb_is_used_in_a_tool_name():
    """The list stays short: a verb no tool name uses any more comes off it."""
    unused = sorted(VERBS_AND_CONNECTIVES - set(tool_words()))
    assert unused == [], f"VERBS_AND_CONNECTIVES lists words no tool uses: {unused}"


def test_the_guard_finds_a_word_with_no_row():
    """The negative self-test: the parser reads rows, the plural rule covers
    `issues` from `**Issue**`, and a word with no row is reported."""
    text = (
        "| Term | Meaning | Not the same as |\n"
        "|---|---|---|\n"
        "| **Issue** | A work item. | A page. |\n"
        "| **Archive** / **unarchive** | Hide and restore. | Deletion. |\n"
    )
    rows = glossary_rows(text)
    words = {
        "list": ["list_issues"],
        "issues": ["list_issues"],
        "archive": ["archive_issue"],
        "unarchive": ["unarchive_issue"],
        "desk": ["my_desk"],
        "my": ["my_desk"],
    }
    assert undefined(words, rows) == ["desk (in my_desk)"]
    assert covering_row("issues", rows) is rows[0]
    assert covering_row("unarchive", rows) is rows[1]
