"""What a start says about its own configuration is true (H18).

WHY (OPS-10): every start logged "set ATHENA_COOKIE_SECURE=1 for any deployment
served over HTTPS", and ``athena-serve`` refuses that setting (it serves direct
HTTP and has no HTTPS mode) — so the one line an operator reads about cookies
told them to do the thing the launcher forbids. It came from building the app,
which every process that imports ``athena.main`` does, so even the launcher's
own start logged it. And a misspelled ``ATHENA_`` variable (say
``ATHENA_ANONYMOUS_READ=false``) was ignored silently: the instance ran with the
default the operator meant to change.

Now the cookie line is logged at the start of serving, and says what is true for
that start; and ``athena-serve`` names, in one warning, every ``ATHENA_``
variable in its environment that the settings registry (``config.SETTINGS``)
does not declare — never its value. The registry already declares every
variable Athena's code reads anywhere, including the ones the MCP server and the
seat doctor read outside ``athena.config`` (``tests/test_settings_registry.py``
holds the code to it), so none of those is ever reported.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re

from fastapi.testclient import TestClient

from athena import config, main, ops
from athena.core import db, deployment, users
from athena.main import create_app

from test_settings_registry import scan


def _cookie_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "ATHENA_COOKIE_SECURE" in r.getMessage()]


def test_building_the_app_says_nothing_about_cookies(tmp_path, caplog, monkeypatch):
    """Importing ``athena.main`` builds an app in every process (the launcher,
    the doctor, a test); that is not a start, so it logs nothing about cookies."""
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.INFO, logger="athena"):
        create_app(tmp_path / "built.db")

    assert _cookie_records(caplog) == []


def test_a_launcher_start_says_cookies_stay_unsecured_and_why(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "SERVED_BY_ATHENA_SERVE", True, raising=False)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)
    app = create_app(
        tmp_path / "launched.db",
        network_mode=deployment.TEST_MODE,
        allowed_authorities=("testserver:80",),
        expected_server=("127.0.0.1", 8000),
    )

    with caplog.at_level(logging.INFO, logger="athena"):
        with TestClient(app):
            pass

    (record,) = _cookie_records(caplog)
    message = record.getMessage()
    assert record.levelno == logging.INFO
    assert "set ATHENA_COOKIE_SECURE=1" not in message
    assert "athena-serve" in message and "refuses ATHENA_COOKIE_SECURE" in message


def test_a_raw_factory_start_warns_only_where_the_setting_applies(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.INFO, logger="athena"):
        with TestClient(create_app(tmp_path / "raw.db")):
            pass

    (record,) = _cookie_records(caplog)
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "TLS-terminating proxy" in message
    assert "athena-serve refuses it" in message


def _launcher(tmp_path, monkeypatch):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = db.connect(db_path)
    db.migrate(conn)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
        commit=True,
    )
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: None)
    for name in list(os.environ):
        if name.startswith("ATHENA_"):
            monkeypatch.delenv(name)


def _variable_warnings(err: str) -> list[str]:
    return [
        line
        for line in err.splitlines()
        if line.startswith("athena-serve: warning:") and "ATHENA_" in line
    ]


def test_an_undeclared_variable_is_named_in_one_warning_without_its_value(
    tmp_path, monkeypatch, capsys
):
    _launcher(tmp_path, monkeypatch)
    monkeypatch.setenv("ATHENA_ANONYMOUS_READ", "value-that-must-not-print")
    monkeypatch.setenv("ATHENA_WEBHOOK_INTERVALS", "also-private")

    assert ops.serve_main([]) == 0

    err = capsys.readouterr().err
    (warning,) = _variable_warnings(err)
    assert "ATHENA_ANONYMOUS_READ, ATHENA_WEBHOOK_INTERVALS" in warning
    assert "not Athena settings" in warning and "docs/OPERATIONS.md" in warning
    assert "value-that-must-not-print" not in err and "also-private" not in err


def test_an_undeclared_variable_is_named_even_when_the_start_is_refused(
    tmp_path, monkeypatch, capsys
):
    """The misspelling is often why the start is refused: say it first."""
    _launcher(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "b" * 32)  # refused below
    monkeypatch.setenv("ATHENA_BOOTSTRAP_TOKN", "x")

    assert ops.serve_main([]) == 1

    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("athena-serve: warning: ATHENA_BOOTSTRAP_TOKN is set")
    assert "must be removed" in lines[-1]


def test_no_variable_the_code_reads_is_reported(tmp_path, monkeypatch, capsys):
    """Every ``ATHENA_*`` variable the registry test finds a read of — in
    ``athena.config``, the MCP server, the seat doctor, the repository's
    scripts — is declared, so setting all of them yields no warning."""
    reads, _ = scan()
    read_names = sorted({read.name for read in reads})
    assert {"ATHENA_TOKEN", "ATHENA_BASE_URL", "ATHENA_MCP_PROFILE"} <= set(read_names)
    assert config.undeclared_variables({name: "x" for name in read_names}) == []

    _launcher(tmp_path, monkeypatch)
    for name in ("ATHENA_TOKEN", "ATHENA_BASE_URL", "ATHENA_MCP_PROFILE"):
        monkeypatch.setenv(name, "x")
    assert ops.serve_main([]) == 0
    assert _variable_warnings(capsys.readouterr().err) == []


def test_only_athena_names_are_considered():
    assert (
        config.undeclared_variables(
            {"PATH": "/bin", "ATHENA": "x", "athena_db": "x", "ATHENA_DB": "x"}
        )
        == []
    )


# --- review of the first build ----------------------------------------------


def test_another_pinned_server_is_not_described_as_athena_serve(
    tmp_path, caplog, monkeypatch
):
    """athena-demo pins its app to a listener too, and its cookies can carry
    Secure; the athena-serve sentence is only true of an athena-serve start."""
    monkeypatch.setattr(config, "SERVED_BY_ATHENA_SERVE", False, raising=False)
    for secure in (True, False):
        monkeypatch.setattr(config, "COOKIE_SECURE", secure)
        monkeypatch.setattr(main, "_cookie_secure_warned", False)
        caplog.clear()
        app = create_app(
            tmp_path / f"demo-{secure}.db",
            network_mode=deployment.TEST_MODE,
            allowed_authorities=("testserver:80",),
            expected_server=("127.0.0.1", 8000),
        )
        with caplog.at_level(logging.INFO, logger="athena"):
            with TestClient(app):
                pass
        assert not [
            r for r in caplog.records if "athena-serve serves" in r.getMessage()
        ], secure


def test_athena_serve_marks_its_own_start(tmp_path, monkeypatch):
    _launcher(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SERVED_BY_ATHENA_SERVE", False, raising=False)
    assert ops.serve_main([]) == 0
    assert config.SERVED_BY_ATHENA_SERVE is True


#: The client-side names the documents and examples tell an operator to set in
#: their own shell, found the way an operator meets them.
_CLIENT_NAME = re.compile(
    r"\$\{?(ATHENA_[A-Z0-9_]*[A-Z0-9])|environ(?:\.get\(|\[)\s*[\"'](ATHENA_[A-Z0-9_]*[A-Z0-9])"
)


def _documented_client_names() -> set[str]:
    repo = Path(__file__).resolve().parent.parent
    files = [*sorted((repo / "docs").glob("*.md")), repo / "README.md"]
    files += sorted((repo / "examples").rglob("*.py"))
    found = set()
    for path in files:
        for match in _CLIENT_NAME.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(1) or match.group(2))
    return found - set(config.SETTINGS)


def test_client_side_names_the_documents_use_are_not_called_misspellings(
    tmp_path, monkeypatch, capsys
):
    documented = _documented_client_names()
    assert {"ATHENA_URL", "ATHENA_ADMIN_TOKEN", "ATHENA_AGENT_TOKEN"} <= documented
    assert set(config.CLIENT_VARIABLES) == documented

    _launcher(tmp_path, monkeypatch)
    for name in documented:
        monkeypatch.setenv(name, "ath_client_side_value")
    assert ops.serve_main([]) == 0
    err = capsys.readouterr().err
    assert _variable_warnings(err) == [] and "ath_client_side_value" not in err


def test_the_doctor_names_an_undeclared_variable_too(tmp_path, monkeypatch, capsys):
    _launcher(tmp_path, monkeypatch)
    monkeypatch.setenv("ATHENA_ANONYMOUS_READ", "value-that-must-not-print")

    ops.doctor_main([str(config.DB_PATH)])

    err = capsys.readouterr().err
    assert any(
        line.startswith("athena-doctor: warning: ATHENA_ANONYMOUS_READ is set")
        for line in err.splitlines()
    ), err
    assert "value-that-must-not-print" not in err
