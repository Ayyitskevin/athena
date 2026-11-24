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
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time

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


def _seed_fleet(tmp_path: Path, *extra: str) -> Path:
    database = tmp_path / "fleet.db"
    seeded = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "seed_benchmark.py"),
            str(database),
            "--shape",
            "fleet",
            "--issues",
            "300",
            "--hot-comments",
            "3",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
    )
    assert seeded.returncode == 0, seeded.stderr[-2000:]
    assert f"seeded {database}" in seeded.stdout
    return database


def test_the_fleet_seed_writes_the_shape_it_promises(tmp_path):
    """H8: the medians in docs/PERFORMANCE_MEDIANS.json are only as reproducible
    as the seed. Read back the shape its docstring promises; the map beside it
    holds bearer tokens, so only its owner may read it."""
    database = _seed_fleet(tmp_path)
    seed_map_path = tmp_path / "fleet.db.json"
    assert stat.S_IMODE(seed_map_path.stat().st_mode) == 0o600
    seed_map = json.loads(seed_map_path.read_text())
    assert seed_map["shape"] == "fleet" and len(seed_map["seats"]) == 6
    assert seed_map["analyzed"] is False
    assert set(seed_map["open_variants"]) == {
        "ready",
        "blocked",
        "yielded_needs_input",
        "completed_left_open",
        "yielded_blocked",
        "archived_open",
        "in_progress",
        "delegated",
    }
    conn = sqlite3.connect(database)
    try:

        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        assert count("SELECT COUNT(*) FROM issues") == 300
        assert count("SELECT COUNT(*) FROM users WHERE is_agent = 1") == 6
        assert count("SELECT COUNT(*) FROM projects WHERE visibility = 'private'") == 1
        # Through the commands: the closed issues carry their whole lifecycle.
        assert count("SELECT COUNT(*) FROM activity WHERE verb = 'claim_completed'") > 0
        assert count("SELECT COUNT(*) FROM issue_claim_handoffs") > 0
        assert count("SELECT COUNT(*) FROM issues WHERE archived_at IS NOT NULL") > 0
        assert count("SELECT COUNT(*) FROM issue_links WHERE kind = 'blocks'") > 0
        assert count("SELECT COUNT(*) FROM issue_contributors") > 0
        assert count("SELECT COUNT(*) FROM issues WHERE status = 'in_progress'") > 0
        assert (
            count("SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'") == 0
        )
    finally:
        conn.close()
    # The map is not something to commit.
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "scratch/fleet.db.json"],
        cwd=REPO,
        check=False,
    )
    assert ignored.returncode == 0


def test_the_fleet_seed_refuses_what_it_would_ignore(tmp_path):
    refused = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "seed_benchmark.py"),
            str(tmp_path / "x.db"),
            "--shape",
            "fleet",
            "--events",
            "10",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert refused.returncode == 2 and "--events" in refused.stderr
    assert not (tmp_path / "x.db").exists()


def _benchmark(*args: str, **kwargs) -> list[str]:
    return [sys.executable, str(REPO / "scripts" / "benchmark_hot_paths.py"), *args]


def test_the_benchmark_runs_records_and_leaves_nothing_behind(tmp_path):
    """Run the benchmark once over a small seed with --record and read the
    file's form back. Nothing here looks at a timing: the gate holds query
    plans, not numbers. Its scratch directory is gone when it returns."""
    database = _seed_fleet(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    medians = tmp_path / "medians.json"
    measured = subprocess.run(
        _benchmark(
            str(database),
            "--calls",
            "1",
            "--rounds",
            "1",
            "--scratch-root",
            str(scratch),
            "--record",
            str(medians),
            "--record-anyway",
            "a test run",
        ),
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
    )
    assert measured.returncode == 0, measured.stderr[-2000:]
    assert list(scratch.iterdir()) == []
    document = json.loads(medians.read_text())
    assert document["schema"] == "athena.hot_path_medians.v1"
    assert document["recorded"] is True
    assert document["workspace"]["issues"] == 300
    paths = document["paths"]
    assert "GET /desk (seat)" in paths and "POST /issues/claim-next (seat)" in paths
    assert all(row["status"] in (200, 201) for row in paths.values() if "status" in row)
    reads = [row for name, row in paths.items() if not name.startswith("POST")]
    assert all(row["n"] == 1 and row["median_ms"] >= 0 for row in reads)
    if document["recorded_despite"] is not None:
        assert document["recorded_despite"]["reason"] == "a test run"
    # The tracked file is in the same form; until it is recorded it holds none.
    tracked = json.loads((REPO / "docs" / "PERFORMANCE_MEDIANS.json").read_text())
    assert tracked["schema"] == document["schema"]
    if tracked["recorded"]:
        assert set(tracked["paths"]) == set(paths)
    else:
        assert tracked["paths"] == {}


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state != "Z"


def _wait_for(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        threading.Event().wait(0.1)
    return condition()


def test_a_killed_benchmark_leaves_no_server_and_no_scratch(tmp_path):
    """SIGKILL runs no cleanup, and a test's subprocess timeout sends one. The
    server is in its own process group and a watcher removes it and the scratch
    directory when the benchmark's end closes the pipe it holds."""
    database = _seed_fleet(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    running = subprocess.Popen(
        _benchmark(str(database), "--calls", "100000", "--scratch-root", str(scratch)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: list(scratch.glob("*/server.pid")), 60)
        pid = int(next(scratch.glob("*/server.pid")).read_text())
        assert _alive(pid)
    finally:
        running.kill()
        running.wait()
    assert _wait_for(lambda: not _alive(pid), 30), f"athena-serve {pid} still runs"
    assert _wait_for(lambda: list(scratch.iterdir()) == [], 30), list(scratch.iterdir())


def test_record_refuses_a_busy_machine_or_a_changed_checkout():
    """A median recorded at a load above the CPU count, or from a checkout
    that is not the commit it names, is not a baseline."""
    spec = importlib.util.spec_from_file_location(
        "benchmark_hot_paths", REPO / "scripts" / "benchmark_hot_paths.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    refusals = module.record_refusals
    assert refusals(load=1.0, cpus=4, commit="abc1234", changes="") == []
    assert refusals(load=24.0, cpus=4, commit="abc1234", changes="") != []
    assert refusals(load=1.0, cpus=4, commit="abc1234", changes=" M src/x.py") != []
    assert refusals(load=1.0, cpus=4, commit=None, changes=None) != []


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
