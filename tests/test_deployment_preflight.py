"""Supported deployment policy and launcher fail closed before serving."""

from __future__ import annotations

import sqlite3
import socket

import pytest

from athena import config, ops
from athena import main as athena_main
from athena.core import db, deployment, oidc, passwords, tokens, users


@pytest.mark.parametrize(
    ("host", "mode", "allowed"),
    [
        ("127.0.0.1", "local", True),
        ("::1", "local", True),
        ("::ffff:127.0.0.1", "local", True),
        ("100.64.0.1", "tailnet", True),
        ("100.127.255.254", "tailnet", True),
        ("fd7a:115c:a1e0::1", "tailnet", True),
        ("127.0.0.1", "tailnet", True),
        ("0.0.0.0", "local", False),
        ("::", "tailnet", False),
        ("::1%lo", "local", False),
        ("192.168.1.5", "tailnet", False),
        ("169.254.1.5", "tailnet", False),
        ("8.8.8.8", "tailnet", False),
        ("athena.internal", "tailnet", False),
    ],
)
def test_address_policy_is_an_explicit_local_or_tailnet_contract(host, mode, allowed):
    assert deployment.address_allowed(host, mode) is allowed


@pytest.mark.parametrize(
    ("raw", "host", "port", "rendered"),
    [
        ("Example.COM:8443", "example.com", 8443, "example.com:8443"),
        ("127.0.0.1:8000", "127.0.0.1", 8000, "127.0.0.1:8000"),
        ("[0:0::1]:8000", "::1", 8000, "[::1]:8000"),
    ],
)
def test_authority_parser_normalizes_exact_dns_and_ip_values(raw, host, port, rendered):
    authority = deployment.parse_authority(raw)
    assert (authority.host, authority.port) == (host, port)
    assert authority.render() == rendered


def test_bind_rejects_an_ipv6_scope_identifier_before_socket_setup():
    with pytest.raises(ValueError, match="scope identifier"):
        deployment.validate_bind("::1%lo", 8000, "local")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "*:8000",
        ".example.com:8000",
        "example.com.:8000",
        "user@example.com:8000",
        "example.com/path:8000",
        "example.com:0",
        "example.com:65536",
        "example.com:+80",
        "127.000.000.001:8000",
        "0x08080808:8000",
        "0x7f000001:8000",
        "0x64400102:8000",
        "0x64.0x40.0x1.0x2:8000",
        "athena.123:8000",
        "athena.010:8000",
        "athena.0x:8000",
        "athena.0x7f:8000",
        "athena.xn--1:8000",
        "xn--example:8000",
        "::1:8000",
        "[::1",
        "[::1]extra:8000",
        "[fe80::1%eth0]:8000",
        "two.example:8000,evil.example:8000",
        "white space:8000",
    ],
)
def test_authority_parser_rejects_ambiguous_or_wildcard_values(raw):
    with pytest.raises(ValueError):
        deployment.parse_authority(raw)


def test_request_host_requires_one_exact_allowed_authority():
    allowed = deployment.normalize_authorities(("athena.tailnet:8443", "[::1]:8000"))
    assert deployment.request_authority_allowed(
        [(b"host", b"ATHENA.TAILNET:8443")],
        scheme="https",
        allowed=allowed,
    )
    assert deployment.request_authority_allowed(
        [(b"host", b"[::1]:8000")],
        scheme="http",
        allowed=allowed,
    )
    for headers in (
        [],
        [(b"host", b"evil.example:8443")],
        [(b"host", b"athena.tailnet")],
        [(b"host", b"athena.tailnet:8443"), (b"host", b"evil.example:8443")],
        [
            (b"host", b"evil.example:8443"),
            (b"x-forwarded-host", b"athena.tailnet:8443"),
        ],
    ):
        assert not deployment.request_authority_allowed(
            headers,
            scheme="https",
            allowed=allowed,
        )


def test_listener_authorities_are_mode_and_port_scoped():
    assert (
        len(
            deployment.validate_authorities(
                (),
                network_mode="local",
                bind_host="127.0.0.1",
                bind_port=8000,
            )
        )
        == 2
    )
    with pytest.raises(ValueError, match="loopback"):
        deployment.validate_authorities(
            ("evil.example:8000",),
            network_mode="local",
            bind_host="127.0.0.1",
            bind_port=8000,
        )
    with pytest.raises(ValueError, match="exact port"):
        deployment.validate_authorities(
            ("localhost:8000", "127.0.0.1:9999"),
            network_mode="local",
            bind_host="127.0.0.1",
            bind_port=8000,
        )
    with pytest.raises(ValueError, match="required"):
        deployment.validate_authorities(
            (),
            network_mode="tailnet",
            bind_host="100.64.1.2",
            bind_port=8000,
        )
    for authority in (
        "0.0.0.0:8000",
        "[::]:8000",
        "224.0.0.1:8000",
        "255.255.255.255:8000",
        "8.8.8.8:8000",
    ):
        with pytest.raises(ValueError, match="Tailscale"):
            deployment.validate_authorities(
                (authority,),
                network_mode="tailnet",
                bind_host="100.64.1.2",
                bind_port=8000,
            )
    assert deployment.validate_authorities(
        ("100.64.1.2:8000", "athena.tailnet:8000"),
        network_mode="tailnet",
        bind_host="100.64.1.2",
        bind_port=8000,
    )
    with pytest.raises(ValueError, match="listener address"):
        deployment.validate_authorities(
            ("100.64.1.3:8000",),
            network_mode="tailnet",
            bind_host="100.64.1.2",
            bind_port=8000,
        )
    with pytest.raises(ValueError, match="loopback listener"):
        deployment.validate_authorities(
            ("localhost:8000",),
            network_mode="tailnet",
            bind_host="100.64.1.2",
            bind_port=8000,
        )
    assert (
        deployment.validate_authorities(
            (),
            network_mode="local",
            bind_host="127.0.0.2",
            bind_port=8000,
        )[0].render()
        == "127.0.0.2:8000"
    )
    with pytest.raises(ValueError, match="localhost"):
        deployment.validate_authorities(
            ("localhost:8000",),
            network_mode="local",
            bind_host="127.0.0.2",
            bind_port=8000,
        )


def test_supported_oidc_recovery_matches_the_direct_listener_boundary():
    authorities = deployment.normalize_authorities(
        ("localhost:8000", "athena.tailnet:8000")
    )
    assert (
        deployment.validate_supported_oidc_recovery(
            issuer="https://idp.example.com/tenant",
            redirect_url="http://localhost:8000/auth/callback",
            allowed_authorities=authorities,
        )
        == "https://idp.example.com/tenant"
    )
    for issuer, redirect, message in (
        (
            "not-a-url",
            "http://localhost:8000/auth/callback",
            "HTTPS issuer",
        ),
        (
            "http://idp.example.com",
            "http://localhost:8000/auth/callback",
            "HTTPS issuer",
        ),
        (
            "https://idp.exa\tmple",
            "http://localhost:8000/auth/callback",
            "visible ASCII",
        ),
        (
            "https://idp.example.com",
            "http://local\nhost:8000/auth/callback",
            "visible ASCII",
        ),
        (
            "https://idp.example.com",
            "https://localhost:8000/auth/callback",
            "direct HTTP",
        ),
        (
            "https://idp.example.com",
            "http://wrong.example:8000/auth/callback",
            "exactly match",
        ),
        (
            "https://idp.example.com",
            "http://localhost:8000/wrong",
            "/auth/callback",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            deployment.validate_supported_oidc_recovery(
                issuer=issuer,
                redirect_url=redirect,
                allowed_authorities=authorities,
            )


def _migrated_database(path):
    conn = db.connect(path)
    db.migrate(conn)
    return conn


def test_active_admin_password_is_a_recovery_path(tmp_path):
    conn = _migrated_database(tmp_path / "athena.db")
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="correct horse battery staple",
        role="admin",
    )
    try:
        assert deployment.has_configured_active_admin_credential(
            conn, enabled_oidc_issuer=None
        )
    finally:
        conn.close()


def test_legacy_password_recovery_requires_the_historical_request_envelope(
    tmp_path,
):
    conn = _migrated_database(tmp_path / "athena.db")
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    bounded_hash = conn.execute(
        "SELECT password_hash FROM users WHERE id = ?",
        (admin["id"],),
    ).fetchone()["password_hash"]
    assert passwords.is_bounded_hash(bounded_hash)
    assert deployment.has_configured_active_admin_credential(
        conn,
        enabled_oidc_issuer=None,
        max_request_body_bytes=deployment.MIN_SUPPORTED_REQUEST_BODY_BYTES,
    )

    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (passwords._encode_password("password", marker=None), admin["id"]),
    )
    conn.commit()
    assert not deployment.has_configured_active_admin_credential(
        conn,
        enabled_oidc_issuer=None,
        max_request_body_bytes=deployment.LEGACY_LOGIN_BODY_BYTES - 1,
    )
    assert deployment.has_configured_active_admin_credential(
        conn,
        enabled_oidc_issuer=None,
        max_request_body_bytes=deployment.LEGACY_LOGIN_BODY_BYTES,
    )
    conn.close()


@pytest.mark.parametrize(
    "email",
    (
        "",
        " \t ",
        "admin",
        " admin@example.com ",
        "x" * (users.MAX_EMAIL_CHARS + 1),
    ),
)
def test_legacy_unaddressable_admin_email_is_not_password_recovery(tmp_path, email):
    conn = _migrated_database(tmp_path / "athena.db")
    users.create_user(
        conn,
        email=email,
        name="Legacy Admin",
        password="password",
        role="admin",
    )
    try:
        assert not deployment.has_configured_active_admin_credential(
            conn,
            enabled_oidc_issuer=None,
        )
    finally:
        conn.close()


def test_only_a_live_admin_scoped_token_counts_as_token_recovery(tmp_path):
    conn = _migrated_database(tmp_path / "athena.db")
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        role="admin",
    )
    read_token = tokens.create_token(
        conn,
        user_id=admin["id"],
        name="read only",
        scopes=["read"],
    )
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer=None
    )
    admin_token = tokens.create_token(
        conn,
        user_id=admin["id"],
        name="recovery",
        scopes=["admin"],
    )
    assert deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer=None
    )
    tokens.revoke_token(
        conn,
        user_id=admin["id"],
        token_id=admin_token["id"],
    )
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer=None
    )
    assert read_token["scopes"] == ["read"]
    conn.close()


def test_only_a_matching_enabled_oidc_identity_counts_as_sso_recovery(tmp_path):
    conn = _migrated_database(tmp_path / "athena.db")
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        role="admin",
    )
    oidc.link_identity(
        conn,
        issuer="https://idp.example",
        subject="admin-subject",
        user_id=admin["id"],
    )
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer=None
    )
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer="https://other-idp.example"
    )
    assert deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer="https://idp.example"
    )
    conn.execute(
        "UPDATE oidc_identities SET subject = ? WHERE user_id = ?",
        (sqlite3.Binary(b"admin-subject"), admin["id"]),
    )
    conn.commit()
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer="https://idp.example"
    )
    users.set_paused(conn, admin["id"], True)
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer="https://idp.example"
    )
    conn.close()


def test_malformed_stored_password_and_token_verifiers_do_not_count_as_recovery(
    tmp_path,
):
    conn = _migrated_database(tmp_path / "athena.db")
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        role="admin",
    )
    conn.execute(
        "UPDATE users SET password_hash = 'malformed' WHERE id = ?",
        (admin["id"],),
    )
    conn.commit()
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer=None
    )

    token = tokens.create_token(
        conn,
        user_id=admin["id"],
        name="malformed recovery",
        scopes=["admin"],
    )
    conn.execute(
        "UPDATE api_tokens SET token_hash = 'malformed' WHERE id = ?",
        (token["id"],),
    )
    conn.commit()
    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer=None
    )
    conn.close()


def test_non_text_stored_token_scopes_do_not_count_as_recovery(tmp_path):
    conn = _migrated_database(tmp_path / "athena.db")
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        role="admin",
    )
    token = tokens.create_token(
        conn,
        user_id=admin["id"],
        name="malformed scopes",
        scopes=["admin"],
    )
    conn.execute(
        "UPDATE api_tokens SET scopes = ? WHERE id = ?",
        (sqlite3.Binary(b"admin"), token["id"]),
    )
    conn.commit()

    assert not deployment.has_configured_active_admin_credential(
        conn, enabled_oidc_issuer=None
    )
    conn.close()


def test_database_posture_distinguishes_bootstrap_and_normal_startup(tmp_path):
    conn = _migrated_database(tmp_path / "athena.db")
    assert "zero users" in deployment.check_database_posture(
        conn,
        bootstrap=True,
        enabled_oidc_issuer=None,
    )
    with pytest.raises(ValueError, match="active administrator"):
        deployment.check_database_posture(
            conn,
            bootstrap=False,
            enabled_oidc_issuer=None,
        )
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    with pytest.raises(ValueError, match="zero users"):
        deployment.check_database_posture(
            conn,
            bootstrap=True,
            enabled_oidc_issuer=None,
        )
    assert "1 active" in deployment.check_database_posture(
        conn,
        bootstrap=False,
        enabled_oidc_issuer=None,
    )
    conn.close()


def _configure_launcher(
    monkeypatch,
    *,
    db_path,
    attach_dir,
    bootstrap_token="",
    network_mode="local",
    authorities=(),
):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", bootstrap_token)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "NETWORK_MODE", network_mode)
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", authorities)


def test_launcher_bootstraps_only_an_empty_local_database_before_running(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap"]) == 0

    assert launched == [{"host": "127.0.0.1", "port": 8000, "fd": None}]
    assert config.ALLOWED_AUTHORITIES == (
        "localhost:8000",
        "127.0.0.1:8000",
    )
    assert "athena-serve: preflight ok" in capsys.readouterr().out
    conn = db.connect(db_path)
    try:
        assert users.count_users(conn) == 0
    finally:
        conn.close()


def test_rejected_bootstrap_does_not_migrate_a_nonempty_database(
    tmp_path, monkeypatch, capsys, migration_inventory_through
):
    source_migrations = db.MIGRATIONS_DIR
    migration_inventory_through("0068_issue_assignee_facts.sql")
    db_path = tmp_path / "athena.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    before = tuple(
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    )
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS_DIR", source_migrations)
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap"]) == 1

    conn = sqlite3.connect(db_path)
    try:
        after = tuple(
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
                "AND name = 'event_sources'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
    assert after == before
    assert launched == []
    assert "zero users" in capsys.readouterr().err


def test_bootstrap_dry_run_rejects_hybrid_schema_before_real_migration(
    tmp_path, monkeypatch, capsys, migration_inventory_through
):
    source_migrations = db.MIGRATIONS_DIR
    migration_inventory_through("0068_issue_assignee_facts.sql")
    db_path = tmp_path / "athena.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute("CREATE TABLE foreign_business_data (secret TEXT NOT NULL)")
    conn.execute("INSERT INTO foreign_business_data VALUES ('preserve me')")
    before = tuple(
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS_DIR", source_migrations)
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap"]) == 1

    conn = sqlite3.connect(db_path)
    try:
        after = tuple(
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
                "AND name = 'event_sources'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT secret FROM foreign_business_data").fetchone() == (
            "preserve me",
        )
    finally:
        conn.close()
    assert after == before
    assert launched == []
    assert "does not match the packaged Athena schema" in capsys.readouterr().err


def test_bootstrap_refuses_an_unrelated_existing_database_without_writing(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "unrelated.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE foreign_business_data (secret TEXT NOT NULL)")
    conn.execute("INSERT INTO foreign_business_data VALUES ('preserve me')")
    conn.commit()
    conn.close()
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap"]) == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT secret FROM foreign_business_data").fetchone()[
            0
        ] == ("preserve me")
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
    assert launched == []
    assert "not recognized as Athena" in capsys.readouterr().err


def test_bootstrap_retries_after_only_the_canonical_empty_ledger_was_committed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version TEXT PRIMARY KEY, "
        "checksum TEXT NOT NULL CHECK(length(checksum) = 64), "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()
    conn.close()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap"]) == 0

    assert launched == [{"host": "127.0.0.1", "port": 8000, "fd": None}]
    conn = db.connect(db_path)
    try:
        assert db.migration_status(conn).pending == ()
        assert users.count_users(conn) == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "filename",
    ("athena#shadow.db", "athena?shadow.db", "athena%23shadow.db"),
)
def test_deployment_database_checks_treat_uri_metacharacters_as_path_bytes(
    tmp_path, filename
):
    db_path = tmp_path / filename
    conn = _migrated_database(db_path)
    conn.execute(
        "UPDATE schema_migrations SET checksum = ? "
        "WHERE version = '0069_event_sources.sql'",
        ("0" * 64,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(db.MigrationIntegrityError, match="checksum mismatch"):
        ops._check_database(db_path, migrate=False)


@pytest.mark.parametrize(
    "filename",
    ("athena#shadow.db", "athena?shadow.db", "athena%23shadow.db"),
)
def test_attachment_checks_treat_uri_metacharacters_as_path_bytes(tmp_path, filename):
    db_path = tmp_path / filename
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = _migrated_database(db_path)
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.execute(
        "INSERT INTO attachments "
        "(target_kind, target_id, filename, content_type, byte_size, sha256, "
        "stored_name, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "issue",
            1,
            "missing.txt",
            "text/plain",
            0,
            "0" * 64,
            "0" * 32,
            admin["id"],
        ),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="missing=1"):
        ops._check_attachment_dir(db_path, attach_dir)


def test_launcher_normal_start_requires_recoverable_admin_and_no_bootstrap_token(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = _migrated_database(db_path)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.close()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main([]) == 1
    assert launched == []
    assert "must be removed" in capsys.readouterr().err

    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    assert ops.serve_main([]) == 0
    assert launched == [{"host": "127.0.0.1", "port": 8000, "fd": None}]


def test_launcher_accepts_a_legacy_admin_hash_at_the_historical_body_cap(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = _migrated_database(db_path)
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        role="admin",
    )
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (passwords._encode_password("password", marker=None), admin["id"]),
    )
    conn.commit()
    conn.close()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
    )
    monkeypatch.setattr(
        config,
        "MAX_REQUEST_BODY_BYTES",
        deployment.LEGACY_LOGIN_BODY_BYTES,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main([]) == 0

    assert launched == [{"host": "127.0.0.1", "port": 8000, "fd": None}]


def test_direct_http_launcher_refuses_secure_only_browser_cookies(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = _migrated_database(db_path)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.close()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
    )
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main([]) == 1

    assert launched == []
    assert "direct-HTTP" in capsys.readouterr().err


def test_launcher_refuses_foreign_key_corruption_before_running(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = _migrated_database(db_path)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.close()
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash, scopes) "
        "VALUES (999, 'orphan', ?, 'admin')",
        ("0" * 64,),
    )
    raw.commit()
    raw.close()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main([]) == 1

    assert launched == []
    assert "foreign key check failed" in capsys.readouterr().err


def test_launcher_refuses_logical_schema_loss_before_running(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = _migrated_database(db_path)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.execute("DROP TABLE sessions")
    conn.commit()
    conn.close()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main([]) == 1

    assert launched == []
    assert "does not match the packaged Athena schema" in capsys.readouterr().err


def test_launcher_rejects_wildcard_before_creating_database(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap", "--host", "0.0.0.0"]) == 1

    assert not db_path.exists()
    assert launched == []
    assert "not permitted" in capsys.readouterr().err


def test_launcher_refuses_an_unusable_body_cap_before_bootstrap_writes(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 1)
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap"]) == 1

    assert not db_path.exists()
    assert launched == []
    assert "at least 16384" in capsys.readouterr().err


def test_launcher_refuses_an_oidc_callback_outside_its_boundary_before_writes(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    monkeypatch.setattr(config, "OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "client")
    monkeypatch.setattr(config, "OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        config,
        "OIDC_REDIRECT_URL",
        "http://wrong.example:8000/auth/callback",
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--bootstrap"]) == 1

    assert not db_path.exists()
    assert launched == []
    assert "exactly match" in capsys.readouterr().err


def test_tailnet_launcher_requires_exact_authority_and_positive_limits(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = _migrated_database(db_path)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.close()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        network_mode="tailnet",
        authorities=("athena.tailnet:8443",),
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    assert ops.serve_main(["--host", "100.64.1.2", "--port", "8443"]) == 1
    assert "ATHENA_ANON_RATE_LIMIT_PER_MINUTE" in capsys.readouterr().err
    assert launched == []

    monkeypatch.setattr(config, "ANON_RATE_LIMIT_PER_MINUTE", 120)
    assert ops.serve_main(["--host", "100.64.1.2", "--port", "8443"]) == 0
    assert launched == [{"host": "100.64.1.2", "port": 8443, "fd": None}]


def test_launcher_inspects_and_preserves_an_inherited_listener(tmp_path, monkeypatch):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        assert ops.serve_main(["--bootstrap", "--fd", str(listener.fileno())]) == 0
        assert launched == [
            {
                "host": "127.0.0.1",
                "port": listener.getsockname()[1],
                "fd": listener.fileno(),
            }
        ]
        assert listener.getsockname()[0] == "127.0.0.1"
    finally:
        listener.close()


def test_launcher_refuses_an_out_of_range_listener_descriptor(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    _configure_launcher(
        monkeypatch,
        db_path=db_path,
        attach_dir=attach_dir,
        bootstrap_token="b" * 32,
    )

    assert ops.serve_main(["--bootstrap", "--fd", str(1 << 100)]) == 1

    assert not db_path.exists()
    assert "file descriptor is out of range" in capsys.readouterr().err


def test_run_server_builds_a_fresh_app_pinned_to_the_preflighted_listener(
    monkeypatch,
):
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(
        config,
        "ALLOWED_AUTHORITIES",
        ("localhost:8123", "127.0.0.1:8123"),
    )
    created: list[tuple[object, dict]] = []

    def create_app(**kwargs):
        application = object()
        created.append((application, kwargs))
        return application

    runs: list[tuple[object, dict]] = []
    monkeypatch.setattr(athena_main, "create_app", create_app)
    monkeypatch.setattr(
        "uvicorn.run",
        lambda application, **kwargs: runs.append((application, kwargs)),
    )

    ops._run_server(host="127.0.0.1", port=8123, fd=None)
    ops._run_server(host="127.0.0.1", port=8123, fd=9)

    assert len(created) == 2
    assert created[0][0] is not created[1][0]
    expected_app_arguments = {
        "network_mode": "local",
        "allowed_authorities": ("localhost:8123", "127.0.0.1:8123"),
        "expected_server": ("127.0.0.1", 8123),
    }
    assert created[0][1] == expected_app_arguments
    assert created[1][1] == expected_app_arguments
    common_server_arguments = {
        "workers": 1,
        "reload": False,
        "lifespan": "on",
        "proxy_headers": False,
        "server_header": False,
        "limit_concurrency": 100,
        "timeout_graceful_shutdown": 30,
    }
    assert runs == [
        (
            created[0][0],
            {
                "host": "127.0.0.1",
                "port": 8123,
                **common_server_arguments,
            },
        ),
        (
            created[1][0],
            {
                "fd": 9,
                **common_server_arguments,
            },
        ),
    ]
