"""Transports write through commands and designated writers, never directly."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_write_ownership.py"


ACTIVITY_SOURCES = {
    "core.activity": (
        "from athena.core import activity_chain\n"
        "def record(conn):\n"
        "    with conn:\n"
        "        cur = conn.execute('INSERT INTO activity (verb) VALUES (?)', ('created',))\n"
        "        event_id = cur.lastrowid\n"
        "        assert event_id is not None\n"
        "        activity_chain.append_entry(conn, event_id)\n"
    ),
    "core.portability": (
        "from athena.core import activity_chain\n"
        "def _insert_activity(conn, events):\n"
        "    for verb in events:\n"
        "        cur = conn.execute('INSERT INTO activity (verb) VALUES (?)', (verb,))\n"
        "        event_id = cur.lastrowid\n"
        "        assert event_id is not None\n"
        "        activity_chain.append_entry(conn, event_id)\n"
        "def replay_import_manifest(conn, events):\n"
        "    with conn:\n"
        "        _insert_activity(conn, events)\n"
    ),
}


def _package(
    tmp_path: Path, modules: dict[str, str], *, include_activity: bool = True
) -> Path:
    package_root = tmp_path / "src" / "athena"
    for area in ("core", "aegis", "web"):
        package = package_root / area
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    sources = {**ACTIVITY_SOURCES, **modules} if include_activity else modules
    for module, source in sources.items():
        path = package_root.joinpath(*module.split(".")).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return package_root


def _run(package_root: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT)]
    if package_root is not None:
        command.extend(("--package-root", str(package_root)))
    return subprocess.run(
        command, cwd=ROOT, check=False, capture_output=True, text=True
    )


def test_current_source_obeys_write_ownership():
    result = _run()

    assert result.returncode == 0, result.stderr
    assert "no" not in result.stderr


def test_an_internal_third_activity_writer_is_refused(tmp_path):
    # Internal modules are outside the transport check. The trail must not gain
    # a new append path just because no HTTP handler calls it directly.
    allowed = _package(tmp_path / "allowed", {})
    assert _run(allowed).returncode == 0
    planted = _package(
        tmp_path / "planted",
        {
            "core.surprise": (
                "def append(conn):\n"
                "    conn.execute('INSERT INTO activity (verb) VALUES (?)', ('lost',))\n"
            )
        },
    )

    result = _run(planted)

    assert result.returncode == 1
    assert "undeclared-activity-writer" in result.stderr
    assert "athena.core.surprise.append" in result.stderr


def test_two_declared_activity_append_paths_pass(tmp_path):
    result = _run(_package(tmp_path, {}))

    assert result.returncode == 0, result.stderr
    assert "activity append census matches" in result.stdout


@pytest.mark.parametrize("module_missing", [False, True])
def test_a_stale_activity_declaration_is_refused(tmp_path, module_missing):
    package_root = _package(tmp_path, {})
    assert _run(package_root).returncode == 0
    native_path = package_root / "core" / "activity.py"
    if module_missing:
        native_path.unlink()
    else:
        native_path.write_text("def record(conn):\n    return None\n")

    result = _run(package_root)

    assert result.returncode == 1
    assert "stale-activity-writer" in result.stderr
    assert "athena.core.activity.record" in result.stderr


def test_an_empty_activity_census_is_not_a_clean_census(tmp_path):
    assert _run(_package(tmp_path / "control", {})).returncode == 0

    result = _run(_package(tmp_path / "empty", {}, include_activity=False))

    assert result.returncode == 1
    assert "empty activity append census" in result.stderr


@pytest.mark.parametrize("module", ["core.activity", "core.portability"])
@pytest.mark.parametrize(
    "replacement",
    [
        "pass",
        "conn.commit()\n        activity_chain.append_entry(conn, event_id)",
        "conn.execute('COMMIT')\n        activity_chain.append_entry(conn, event_id)",
        "activity_chain.append_entry(other_conn, event_id)",
        "activity_chain.append_entry(conn, 999)",
        "if enabled:\n            activity_chain.append_entry(conn, event_id)",
        "def unused():\n            activity_chain.append_entry(conn, event_id)",
        "return\n        activity_chain.append_entry(conn, event_id)",
        "event_id = 999\n        activity_chain.append_entry(conn, event_id)",
    ],
)
def test_declared_activity_insert_must_chain_its_row_before_leaving_the_block(
    tmp_path, module, replacement
):
    # The legitimate native and imported shapes pass. The bad cases must not
    # satisfy a guard merely by mentioning append_entry somewhere in a function.
    assert _run(_package(tmp_path / "control", {})).returncode == 0
    original = ACTIVITY_SOURCES[module]
    call = "activity_chain.append_entry(conn, event_id)"
    assert original.count(call) == 1
    broken = _package(
        tmp_path / "broken", {module: original.replace(call, replacement)}
    )

    result = _run(broken)

    assert result.returncode == 1
    assert "activity-chain-pair" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "def append(conn):\n    conn.execute('insert into activity values (1)')\n",
        "def append(conn):\n    conn.execute('INSERT INTO \"activity\" VALUES (1)')\n",
        "def outer():\n    def record(conn):\n        conn.execute('INSERT INTO activity VALUES (1)')\n",
        "sql = 'INSERT INTO activity VALUES (1)'\n",
    ],
)
def test_the_activity_census_does_not_miss_literal_variants(tmp_path, source):
    # A similarly named table is not the activity trail.
    control = _package(
        tmp_path / "control",
        {"core.other": "sql = 'INSERT INTO activity_chain VALUES (1)'\n"},
    )
    assert _run(control).returncode == 0

    result = _run(_package(tmp_path / "bad", {"core.other": source}))

    assert result.returncode == 1
    assert "undeclared-activity-writer" in result.stderr


def test_command_module_call_is_the_doctrine_working(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.issues": (
                "def create_issue(conn, title):\n"
                "    conn.execute('INSERT INTO issues (title) VALUES (?)', (title,))\n"
            ),
            "core.issue_commands": (
                "from athena.core import issues\n"
                "def create(conn, actor, title):\n"
                "    return issues.create_issue(conn, title)\n"
            ),
            "web.views": (
                "from athena.core import issue_commands\n"
                "def post(conn, actor, title):\n"
                "    return issue_commands.create(conn, actor, title)\n"
            ),
        },
    )

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


def test_transport_calling_data_module_writer_fails(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.issues": (
                "def create_issue(conn, title):\n"
                "    conn.execute('INSERT INTO issues (title) VALUES (?)', (title,))\n"
            ),
            "web.views": (
                "from athena.core import issues\n"
                "def post(conn, title):\n"
                "    return issues.create_issue(conn, title)\n"
            ),
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "data-module-write" in result.stderr
    assert "athena.core.issues.create_issue" in result.stderr


def test_transport_executing_write_sql_fails(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "web.views": (
                "def post(conn, title):\n"
                "    conn.execute('INSERT INTO issues (title) VALUES (?)', (title,))\n"
            ),
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "direct-write-sql" in result.stderr


def test_prose_starting_with_a_write_word_is_not_sql(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "web.views": (
                "def delete_rule(conn, rule_id):\n"
                '    """Delete an automation rule."""\n'
                "    return None\n"
            ),
        },
    )

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


def test_writer_hidden_behind_a_data_module_helper_fails(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.issues": (
                "def _insert(conn, title):\n"
                "    conn.execute('INSERT INTO issues (title) VALUES (?)', (title,))\n"
                "def create_issue(conn, title):\n"
                "    return _insert(conn, title)\n"
            ),
            "aegis.widgets_api": (
                "from athena.core.issues import create_issue\n"
                "def post(conn, title):\n"
                "    return create_issue(conn, title)\n"
            ),
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "athena.core.issues.create_issue" in result.stderr


def test_relative_import_does_not_hide_the_writer(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.issues": (
                "def create_issue(conn, title):\n"
                "    conn.execute('INSERT INTO issues (title) VALUES (?)', (title,))\n"
            ),
            "web.views": (
                "from ..core import issues\n"
                "def post(conn, title):\n"
                "    return issues.create_issue(conn, title)\n"
            ),
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "data-module-write" in result.stderr


def test_allowlisted_writer_passes_only_from_its_transport(tmp_path):
    sessions_module = (
        "def create_session(conn, user_id):\n"
        "    conn.execute('INSERT INTO sessions (user_id) VALUES (?)', (user_id,))\n"
        "def destroy_session(conn, raw):\n"
        "    conn.execute('DELETE FROM sessions WHERE session_hash = ?', (raw,))\n"
    )
    allowed = _package(
        tmp_path,
        {
            "core.sessions": sessions_module,
            "web.auth": (
                "from athena.core import sessions\n"
                "def login(conn, user_id):\n"
                "    return sessions.create_session(conn, user_id)\n"
                "def logout(conn, raw):\n"
                "    sessions.destroy_session(conn, raw)\n"
            ),
        },
    )
    wrong_transport = _package(
        tmp_path / "other",
        {
            "core.sessions": sessions_module,
            "web.auth": (
                "from athena.core import sessions\n"
                "def logout(conn, raw):\n"
                "    sessions.destroy_session(conn, raw)\n"
            ),
            "web.admin": (
                "from athena.core import sessions\n"
                "def reset(conn, user_id):\n"
                "    return sessions.create_session(conn, user_id)\n"
            ),
        },
    )

    assert _run(allowed).returncode == 0
    denied = _run(wrong_transport)
    assert denied.returncode == 1
    assert "data-module-write" in denied.stderr


def test_allowlist_entry_without_a_live_call_fails_as_stale(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.sessions": (
                "def create_session(conn, user_id):\n"
                "    conn.execute('INSERT INTO sessions (user_id) VALUES (?)', (user_id,))\n"
            ),
            "web.auth": "def login(conn):\n    return None\n",
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "stale-allowlist-entry" in result.stderr
    assert "athena.core.sessions.create_session" in result.stderr


def test_diagnostics_are_deterministic(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.issues": (
                "def create_issue(conn, title):\n"
                "    conn.execute('INSERT INTO issues (title) VALUES (?)', (title,))\n"
            ),
            "web.zeta": (
                "from athena.core import issues\n"
                "def post(conn, title):\n"
                "    return issues.create_issue(conn, title)\n"
            ),
            "web.alpha": (
                "def post(conn, title):\n"
                "    conn.execute('DELETE FROM issues WHERE title = ?', (title,))\n"
            ),
        },
    )
    env = {**os.environ, "PYTHONHASHSEED": "1"}
    first = subprocess.run(
        [sys.executable, str(SCRIPT), "--package-root", str(package_root)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    env["PYTHONHASHSEED"] = "999"
    second = subprocess.run(
        [sys.executable, str(SCRIPT), "--package-root", str(package_root)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert first.returncode == second.returncode == 1
    assert first.stderr == second.stderr
    assert first.stderr.index("web/alpha.py") < first.stderr.index("web/zeta.py")


def test_malformed_python_fails_loudly(tmp_path):
    package_root = _package(tmp_path, {"web.broken": "def broken(:\n"})

    result = _run(package_root)

    assert result.returncode == 1
    assert "cannot parse" in result.stderr
