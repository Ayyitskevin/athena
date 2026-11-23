"""The scripts the documents tell a reader to run, run (F4).

``scripts/seed_benchmark.py`` is the script the performance numbers in
``src/athena/main.py`` and ``docs/plans/OPUS_PERFORMANCE_ADOPTION_GUIDE_ATHENA.md``
call reproducible. It stopped running when the writers it calls took a required
``commit`` keyword, and nothing noticed, because no test ran it. The first test
runs it at its smallest size into a temporary database and reads back what it
wrote: a benchmark seed that "runs" but writes nothing would reproduce nothing.

The second test holds every other documented script to the least a reader can
expect: ``--help`` answers with a usage line and exit 0, quickly, without doing
the script's work. Importing the script and building its parser is where a
renamed module, a moved function or a broken import shows first. A script that
ignores ``--help`` and runs its workload instead fails here, since a reader who
asks a script what it does should not find it has already done it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = re.compile(r"(?<![\w/.-])scripts/[A-Za-z0-9_][A-Za-z0-9_/-]*\.py\b")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCE_LINE = re.compile(r"^\s*(`{3,}|~{3,})\s*(\S*)")

#: Scripts a document names in code that are not commands, each with why. Keep
#: this short: a documented script that cannot answer ``--help`` is the defect
#: this test exists to find.
NOT_COMMANDS = {
    "scripts/documentary_review/closeout.py": (
        "a library module of the documentary reviewer (closeout_view), imported by "
        "tests/test_documentary_reader.py; it has no entry point, and "
        "docs/CLOSEOUT_RECIPE.md describes what it renders, not a command to run"
    ),
    "scripts/select_tests.py": (
        "the changed profile's selector, imported by scripts/gate.py; it has no "
        "entry point, and CONTRIBUTING.md names it to say how the selection is "
        "made. The command is scripts/gate.sh --profile changed"
    ),
}


def _tracked_markdown() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    ).stdout.split()
    return [REPO / name for name in listed]


def _code_text(text: str) -> str:
    """The parts of a Markdown document a reader would type: fenced blocks and
    inline code spans.

    Lenient where ``doc_examples.fenced_blocks`` is strict: that reads the five
    entry documents, this reads every tracked document, including ones that
    nest a fence in a longer one or indent it under a list item.
    """
    code: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        opening = _FENCE_LINE.match(line)
        if fence is None:
            if opening:
                fence = opening.group(1)
            else:
                prose.append(line)
        elif opening and opening.group(1).startswith(fence) and not opening.group(2):
            fence = None
        else:
            code.append(line)
    code.extend(_CODE_SPAN.findall("\n".join(prose)))
    return "\n".join(code)


def documented_scripts() -> dict[str, list[str]]:
    """Every ``scripts/*.py`` path shown in code in a tracked Markdown document,
    with the documents that show it."""
    found: dict[str, list[str]] = {}
    for path in _tracked_markdown():
        if not path.exists():  # deleted in the worktree, not yet in the index
            continue
        for name in set(_SCRIPT.findall(_code_text(path.read_text("utf-8")))):
            found.setdefault(name, []).append(str(path.relative_to(REPO)))
    return found


def test_the_benchmark_seed_runs_at_its_smallest_size(tmp_path):
    database = tmp_path / "bench.db"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "seed_benchmark.py"),
            str(database),
            "--issues",
            "1",
            "--events",
            "10",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert f"seeded {database}" in result.stdout

    conn = sqlite3.connect(database)
    try:

        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        # The shape the module docstring promises: four actors, mixed public and
        # private containers, the issues and events asked for, through the real
        # recorders (so the trail carries its hash chain).
        assert count("SELECT COUNT(*) FROM users") == 4
        assert count("SELECT COUNT(*) FROM projects") == 12
        assert count("SELECT COUNT(*) FROM projects WHERE visibility='private'") == 4
        assert count("SELECT COUNT(*) FROM spaces") == 5
        assert count("SELECT COUNT(*) FROM spaces WHERE visibility='private'") == 2
        assert count("SELECT COUNT(*) FROM issues") == 1
        assert count("SELECT COUNT(*) FROM pages") == 500
        details = {
            row[0]
            for row in conn.execute(
                "SELECT detail FROM activity WHERE detail LIKE 'detail %'"
            )
        }
        assert details == {f"detail {i}" for i in range(10)}
        # Seeding never runs ANALYZE by default: see the script's docstring.
        assert (
            count("SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'") == 0
        )
    finally:
        conn.close()


def test_the_documents_name_scripts_that_exist():
    documented = documented_scripts()
    assert "scripts/seed_benchmark.py" in documented, (
        "the scan no longer sees a script the performance guide names; the "
        "pattern or the document changed"
    )
    missing = {
        name: docs for name, docs in documented.items() if not (REPO / name).is_file()
    }
    assert not missing, f"documents show scripts that do not exist: {missing}"
    stale = set(NOT_COMMANDS) - set(documented)
    assert not stale, f"NOT_COMMANDS names scripts no document shows: {stale}"


def _help(name: str) -> tuple[str, subprocess.CompletedProcess[str]]:
    return name, subprocess.run(
        [sys.executable, str(REPO / name), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )


def test_every_documented_script_answers_help():
    names = sorted(set(documented_scripts()) - set(NOT_COMMANDS))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(_help, names))
    failures = {
        name: (result.returncode, (result.stderr or result.stdout)[-400:])
        for name, result in results.items()
        if result.returncode != 0 or "usage:" not in result.stdout
    }
    assert not failures, (
        "documented scripts that do not answer --help with a usage line and "
        f"exit 0 (a script that ran its workload instead counts): {failures}"
    )
