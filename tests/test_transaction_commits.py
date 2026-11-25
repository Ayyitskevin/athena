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
    assert "nothing commits on its own or manages a transaction by hand" in (
        result.stdout
    )


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
    found = _violations(tmp_path, {"aegis.commands": _command(body)})
    # A nested def or lambda does not run in the block; its own commit is a
    # finding of its own scope (G2, below), never of the command.
    assert [v for v in found if v.function == "athena.aegis.commands.command"] == []


def test_asking_a_writer_to_commit_outside_any_block_passes(tmp_path):
    # The writer takes commit and commits only when asked; outside a block the
    # caller may ask. (Inside one, the cases above fail.)
    source = (
        "from athena.core import activity\n"
        "def standalone(conn):\n"
        "    activity.record(conn, verb='v')\n"
        "    activity.record(conn, verb='w', commit=True)\n"
    )
    assert _violations(tmp_path, {"aegis.commands": source}) == []


# --- G2: nothing outside core/db.py commits on its own or opens a transaction


def test_an_unconditional_commit_outside_any_block_fails(tmp_path):
    # The shape G2 removes: a writer with no commit parameter that commits
    # whatever its caller is doing. Called inside a command it would commit
    # the command's first half.
    source = (
        "def unwatch(conn, user_id):\n"
        "    conn.execute('DELETE FROM watches WHERE user_id = ?', (user_id,))\n"
        "    conn.commit()\n"
    )
    found = _violations(tmp_path, {"core.notifications": source})

    assert [(v.function, v.line) for v in found] == [
        ("athena.core.notifications.unwatch", 3)
    ]
    assert "conn.commit() commits on its own" in found[0].detail


@pytest.mark.parametrize(
    "statement",
    [
        "conn.execute('BEGIN IMMEDIATE')",
        "conn.execute('begin')",
        "conn.execute('  BEGIN DEFERRED TRANSACTION')",
        "conn.execute(f'SAVEPOINT {name}')",
        "conn.execute('RELEASE SAVEPOINT s')",
        "conn.execute('ROLLBACK')",
        "conn.executescript('BEGIN; UPDATE t SET x = 1; COMMIT;')",
    ],
)
def test_a_transaction_managed_by_hand_fails(tmp_path, statement):
    # start_sprint's and update_page's shape: BEGIN IMMEDIATE ... commit() under
    # `if commit`. The commit is governed, the BEGIN is not db.transaction's.
    source = (
        "def start(conn, name='s', *, commit=True):\n"
        "    if commit:\n"
        f"        {statement}\n"
        "    conn.execute('UPDATE sprints SET state = 1')\n"
        "    if commit:\n"
        "        conn.commit()\n"
    )
    found = _violations(tmp_path, {"aegis.sprints": source})

    assert [(v.function, v.line) for v in found] == [("athena.aegis.sprints.start", 3)]
    assert "manages a transaction by hand" in found[0].detail


def test_a_hand_opened_transaction_inside_a_block_is_reported_once(tmp_path):
    found = _violations(
        tmp_path, {"aegis.commands": _command("conn.execute('COMMIT')")}
    )
    assert len(found) == 1
    assert "commits inside db.transaction" in found[0].detail


@pytest.mark.parametrize(
    "source, function",
    [
        # A method, a nested function, a lambda, a class body, the module body.
        (
            "class Store:\n    def save(self, conn):\n        conn.commit()\n",
            "athena.aegis.data.Store.save",
        ),
        (
            "def outer(conn):\n    def inner():\n        conn.commit()\n    inner()\n",
            "athena.aegis.data.outer.inner",
        ),
        ("def later(conn):\n    return lambda: conn.commit()\n", None),
        ("class Boot:\n    CONN.commit()\n", "athena.aegis.data.Boot"),
        ("CONN.execute('INSERT INTO t VALUES (1)')\nCONN.commit()\n", None),
        (
            "class Store:\n    def begin(self, conn):\n"
            "        conn.execute('BEGIN IMMEDIATE')\n",
            "athena.aegis.data.Store.begin",
        ),
    ],
    ids=["method", "nested", "lambda", "class-body", "module-body", "method-begin"],
)
def test_a_commit_in_any_scope_outside_core_db_fails(tmp_path, source, function):
    found = _violations(tmp_path, {"aegis.data": source})

    assert len(found) == 1, found
    if function is not None:
        assert found[0].function == function


@pytest.mark.parametrize(
    "source",
    [
        # Governed: commits only when its caller asks.
        "def watch(conn, *, commit):\n"
        "    conn.execute('INSERT INTO watches VALUES (1)')\n"
        "    if commit:\n"
        "        conn.commit()\n",
        # Governed by an early exit.
        "def watch(conn, *, commit=True):\n"
        "    conn.execute('INSERT INTO watches VALUES (1)')\n"
        "    if not commit:\n"
        "        return\n"
        "    conn.commit()\n",
        # Through db.transaction: it owns the commit, and composes.
        "from athena.core import db\n"
        "def unwatch(conn):\n"
        "    with db.transaction(conn):\n"
        "        conn.execute('DELETE FROM watches')\n",
        "from athena.core import db\n"
        "def start(conn, *, commit):\n"
        "    if commit:\n"
        "        with db.transaction(conn, immediate=True):\n"
        "            return start(conn, commit=False)\n"
        "    conn.execute('UPDATE sprints SET state = 1')\n",
        # SQL that is not a transaction statement.
        "def read(conn):\n"
        "    conn.execute('SELECT 1 AS begin_at')\n"
        "    conn.execute('UPDATE t SET ended = 1')\n",
    ],
    ids=["if-commit", "early-exit", "db-transaction", "recursive-block", "plain-sql"],
)
def test_a_writer_that_honours_commit_or_uses_db_transaction_passes(tmp_path, source):
    assert _violations(tmp_path, {"core.notifications": source}) == []


def test_a_local_named_commit_governs_nothing(tmp_path):
    # Only the function's own commit parameter governs a commit point.
    source = (
        "def flush(conn):\n    commit = True\n    if commit:\n        conn.commit()\n"
    )
    assert len(_violations(tmp_path, {"aegis.data": source})) == 1


def test_core_db_may_manage_its_own_transactions(tmp_path):
    db_source = _DB + (
        "def checkpoint(conn):\n"
        "    conn.execute('BEGIN IMMEDIATE')\n"
        "    conn.commit()\n"
    )
    assert _violations(tmp_path, {"core.db": db_source}) == []


_SNAPSHOT = (
    "def snapshot(conn):\n"
    "    conn.execute('BEGIN')\n"
    "    conn.rollback()\n"
    "def stamp(conn):\n"
    "    conn.execute('UPDATE tokens SET used = 1')\n"
    "    conn.commit()\n"
)


def test_an_allowed_function_passes(tmp_path):
    root = _package(tmp_path, {"core.export": _SNAPSHOT})
    allowed = {
        "athena.core.export.snapshot": "a read snapshot",
        "athena.core.export.stamp": "advisory",
    }
    assert checker.find_violations(root, allowed=allowed) == []
    # And only the functions it names.
    found = checker.find_violations(root, allowed={"athena.core.export.stamp": "x"})
    assert [v.function for v in found] == ["athena.core.export.snapshot"]


def test_an_allowed_function_called_inside_a_block_still_fails(tmp_path):
    command = _command(
        "export.stamp(conn)",
        imports="from athena.core import db, export\n",
    )
    root = _package(tmp_path, {"core.export": _SNAPSHOT, "aegis.commands": command})
    allowed = {
        "athena.core.export.snapshot": "a read snapshot",
        "athena.core.export.stamp": "advisory",
    }
    found = checker.find_violations(root, allowed=allowed)
    assert [v.function for v in found] == ["athena.aegis.commands.command"]
    assert "athena.core.export.stamp commits whatever" in found[0].detail


@pytest.mark.parametrize(
    "name, reason",
    [
        ("athena.core.export.gone", "no such function"),
        ("athena.core.export.converted", "no longer commits"),
        ("athena.core.export", "no such function"),
    ],
)
def test_a_stale_allow_list_entry_fails(tmp_path, name, reason):
    source = _SNAPSHOT + (
        "from athena.core import db\n"
        "def converted(conn):\n"
        "    with db.transaction(conn):\n"
        "        conn.execute('UPDATE t SET x = 1')\n"
    )
    root = _package(tmp_path, {"core.export": source})
    allowed = {
        "athena.core.export.snapshot": "a read snapshot",
        "athena.core.export.stamp": "advisory",
        name: "was needed once",
    }
    found = checker.find_violations(root, allowed=allowed)
    assert [v.function for v in found] == [name]
    assert "stale allow-list entry" in found[0].detail
    assert reason in found[0].detail


def test_the_allow_list_is_short_reasoned_and_only_for_this_package(tmp_path):
    for name, reason in checker.ALLOWED_OWN_TRANSACTIONS.items():
        assert name.startswith("athena.") and len(reason.split()) >= 10, name
    # Another tree is checked without it, so its entries are not reported stale.
    root = _package(tmp_path, {})
    assert checker.find_violations(root) == []


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

    in_command = [v for v in found if v.function == "athena.aegis.commands.command"]
    if safe:
        assert found == []
    else:
        assert len(in_command) == 1
        assert "athena.aegis.data.remove commits whatever its commit argument" in (
            in_command[0].detail
        )
        # Where the helper commits by itself, that commit is its own finding too.
        assert {v.function for v in found} <= {
            "athena.aegis.commands.command",
            "athena.aegis.data.remove",
        }


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
