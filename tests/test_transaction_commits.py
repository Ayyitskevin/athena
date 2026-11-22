"""Nothing inside a ``db.transaction`` block commits (B5).

WHY: a command's write and its activity event commit together or not at all.
A helper that commits underneath the block (its ``commit`` defaulted to true,
or it ignored a false one) splits that unit: when the rest of the block then
fails, the first half has already landed. Review ARCH-1 was that shape (a
project lifecycle event committed ahead of the delete it described), and so
was ``dependencies.remove_link``, which committed whatever it was passed.

``scripts/check_transaction_commits.py`` finds the static cases. These tests
pin that it passes on the source, runs on its own (as the gate and CI run it),
and fails each shape it exists to catch, while the safe shapes beside them
pass.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_transaction_commits.py"

_DB = (
    "from contextlib import contextmanager\n"
    "@contextmanager\n"
    "def transaction(conn, *, immediate=False):\n"
    "    conn.execute('BEGIN')\n"
    "    yield\n"
    "    conn.commit()\n"
)
_ACTIVITY = (
    "def record(conn, *, verb, commit=True):\n"
    "    conn.execute('INSERT INTO activity (verb) VALUES (?)', (verb,))\n"
    "    if commit:\n"
    "        conn.commit()\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("check_transaction_commits", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load()


def _package(tmp_path: Path, modules: dict[str, str]) -> Path:
    root = tmp_path / "src" / "athena"
    sources = {"core.db": _DB, "core.activity": _ACTIVITY, **modules}
    for package in ("", "core", "aegis"):
        (root / package).mkdir(parents=True, exist_ok=True)
        (root / package / "__init__.py").write_text("", encoding="utf-8")
    for module, source in sources.items():
        path = root.joinpath(*module.split(".")).with_suffix(".py")
        path.write_text(source, encoding="utf-8")
    return root


def _command(body: str, imports: str = "from athena.core import activity, db\n"):
    """A command whose transaction block runs ``body`` (indented 8)."""
    lines = "".join(f"        {line}\n" for line in body.splitlines())
    return f"{imports}def command(conn):\n    with db.transaction(conn):\n{lines}"


def _violations(tmp_path: Path, modules: dict[str, str]) -> list:
    return checker.find_violations(_package(tmp_path, modules))


def test_the_check_passes_on_the_source():
    assert checker.find_violations() == []


def test_the_checker_runs_alone():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing inside a db.transaction block commits" in result.stdout


def test_it_finds_the_arch_1_shape_before_wave_0(tmp_path):
    # A recorder with no commit parameter that calls activity.record with its
    # default commit=True, called inside the command's block.
    recorder = (
        "from athena.core import activity\n"
        "def record_project_deleted(conn):\n"
        "    activity.record(conn, verb='deleted_project')\n"
    )
    command = _command(
        "project_activity.record_project_deleted(conn)\n"
        "conn.execute('DELETE FROM projects WHERE id = 1')\n",
        imports="from athena.aegis import project_activity\nfrom athena.core import db\n",
    )

    found = _violations(
        tmp_path,
        {"aegis.project_activity": recorder, "aegis.project_commands": command},
    )

    assert [v.function for v in found] == ["athena.aegis.project_commands.command"]
    assert "record_project_deleted commits whatever its commit argument" in (
        found[0].detail
    )
    assert "calls athena.core.activity.record" in found[0].detail


@pytest.mark.parametrize(
    "body",
    [
        "conn.commit()",
        "conn.execute('COMMIT')",
        "conn.execute('end transaction')",
        "conn.executescript('UPDATE t SET x = 1;')",
        "activity.record(conn, verb='v')",
        "activity.record(conn, verb='v', commit=True)",
        "activity.record(conn, verb='v', commit=flag)",
        "activity.record(conn, verb='v', **options)",
        "if ok:\n    activity.record(conn, verb='v')",
        "try:\n    pass\nfinally:\n    conn.commit()",
        "with open('x'):\n    activity.record(conn, verb='v')",
        "with db.transaction(conn):\n    conn.commit()",
        "rows = [activity.record(conn, verb=v) for v in 'ab']",
    ],
)
def test_a_committing_call_inside_the_block_fails(tmp_path, body):
    found = _violations(tmp_path, {"aegis.commands": _command(body)})

    assert len(found) == 1, found
    assert found[0].function == "athena.aegis.commands.command"


@pytest.mark.parametrize(
    "body",
    [
        "activity.record(conn, verb='v', commit=False)",
        "activity.record(conn, verb='v', commit=False, **options)",
        "conn.execute('INSERT INTO t VALUES (1)')",
        "conn.execute('SELECT 1')",
        "def later():\n    conn.commit()",
        "callback = lambda: conn.commit()",
        "self_like.commit_all()",
    ],
)
def test_a_safe_call_inside_the_block_passes(tmp_path, body):
    assert _violations(tmp_path, {"aegis.commands": _command(body)}) == []


def test_a_committing_call_outside_any_block_passes(tmp_path):
    source = (
        "from athena.core import activity\n"
        "def standalone(conn):\n"
        "    activity.record(conn, verb='v')\n"
        "    conn.commit()\n"
    )
    assert _violations(tmp_path, {"aegis.commands": source}) == []


@pytest.mark.parametrize(
    ("helper", "safe"),
    [
        # Honours its parameter: safe with commit=False.
        (
            "    conn.execute('DELETE FROM t')\n    if commit:\n        conn.commit()\n",
            True,
        ),
        # The remove_link bug: takes commit and commits anyway.
        ("    conn.execute('DELETE FROM t')\n    conn.commit()\n", False),
        # The start_sprint shape: an early exit when asked not to commit.
        ("    if not commit:\n        return 1\n    conn.commit()\n", True),
        # Passes its own flag through: safe with commit=False.
        ("    activity.record(conn, verb='v', commit=commit)\n", True),
        # Forgets to pass it on: the callee commits by default.
        ("    activity.record(conn, verb='v')\n", False),
        # Tests commit the wrong way round: commits exactly when asked not to.
        ("    if not commit:\n        conn.commit()\n", False),
        ("    if commit:\n        pass\n    else:\n        conn.commit()\n", False),
        # The else of `if not commit` runs only when asked to commit.
        ("    if not commit:\n        pass\n    else:\n        conn.commit()\n", True),
        # A test that does not decide on commit governs nothing.
        ("    if commit or opened:\n        conn.commit()\n", False),
    ],
    ids=[
        "honours",
        "ignores",
        "early-exit",
        "passes-through",
        "forgets",
        "inverted",
        "inverted-else",
        "else-of-not",
        "mixed-test",
    ],
)
def test_a_helper_must_honour_commit_false(tmp_path, helper, safe):
    data = (
        "from athena.core import activity\ndef remove(conn, *, commit=True):\n" + helper
    )
    command = _command(
        "data.remove(conn, commit=False)",
        imports="from athena.aegis import data\nfrom athena.core import db\n",
    )

    found = _violations(tmp_path, {"aegis.data": data, "aegis.commands": command})

    if safe:
        assert found == []
    else:
        assert len(found) == 1
        assert "athena.aegis.data.remove commits whatever its commit argument" in (
            found[0].detail
        )


def test_commit_passed_through_inside_a_block_fails(tmp_path):
    # The caller may pass commit=True, which would commit its own block.
    source = (
        "from athena.core import activity, db\n"
        "def command(conn, *, commit=True):\n"
        "    with db.transaction(conn):\n"
        "        activity.record(conn, verb='v', commit=commit)\n"
    )
    found = _violations(tmp_path, {"aegis.commands": source})
    assert len(found) == 1
    assert "when this function's own commit is true" in found[0].detail


@pytest.mark.parametrize("positional", ["True", "False"])
def test_a_positional_commit_argument_is_read(tmp_path, positional):
    data = "def write(conn, commit=True):\n    if commit:\n        conn.commit()\n"
    command = _command(
        f"data.write(conn, {positional})",
        imports="from athena.aegis import data\nfrom athena.core import db\n",
    )
    found = _violations(tmp_path, {"aegis.data": data, "aegis.commands": command})
    assert len(found) == (1 if positional == "True" else 0)


def test_resolution_follows_helpers_to_any_depth(tmp_path):
    chain = (
        "from athena.core import activity\n"
        "def third(conn):\n"
        "    activity.record(conn, verb='v')\n"
        "def second(conn):\n"
        "    third(conn)\n"
        "def first(conn):\n"
        "    return second(conn)\n"
    )
    command = _command(
        "chain.first(conn)",
        imports="from athena.aegis import chain\nfrom athena.core import db\n",
    )
    found = _violations(tmp_path, {"aegis.chain": chain, "aegis.commands": command})
    assert len(found) == 1
    assert "athena.aegis.chain.first" in found[0].detail


@pytest.mark.parametrize(
    "imports, block",
    [
        ("from athena.core.db import transaction\n", "transaction(conn)"),
        ("from athena.core import db as database\n", "database.transaction(conn)"),
        ("import athena.core.db\n", "athena.core.db.transaction(conn)"),
        ("from ..core import db\n", "db.transaction(conn, immediate=True)"),
    ],
)
def test_every_import_form_of_the_block_is_seen(tmp_path, imports, block):
    source = imports + f"def command(conn):\n    with {block}:\n        conn.commit()\n"
    found = _violations(tmp_path, {"aegis.commands": source})
    assert len(found) == 1


def test_a_tree_without_db_transaction_is_refused(tmp_path):
    root = _package(tmp_path, {"core.db": "def connect(path):\n    return None\n"})
    with pytest.raises(checker.TransactionCheckError, match="db.transaction"):
        checker.find_violations(root)


def test_the_command_line_reports_each_finding(tmp_path):
    root = _package(tmp_path, {"aegis.commands": _command("conn.commit()")})
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--package-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "1 violation" in result.stderr
    assert "src/athena/aegis/commands.py:4: athena.aegis.commands.command" in (
        result.stderr
    )
