"""Transports write through commands and designated writers, never directly."""

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_write_ownership.py"


def _package(tmp_path: Path, modules: dict[str, str]) -> Path:
    package_root = tmp_path / "src" / "athena"
    for area in ("core", "aegis", "web"):
        package = package_root / area
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    for module, source in modules.items():
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
