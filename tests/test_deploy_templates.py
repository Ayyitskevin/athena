"""Deploy examples stay reusable, secret-free, and wired to supported gates."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "deploy" / "systemd"


def test_service_template_uses_supported_launcher_and_host_local_environment():
    text = (SYSTEMD / "athena.service.in").read_text(encoding="utf-8")

    assert "EnvironmentFile=@ATHENA_CONFIG_DIR@/athena.env" in text
    assert "@ATHENA_CHECKOUT@/.venv/bin/athena-serve" in text
    assert "uvicorn athena.main:app" not in text
    assert "User=@ATHENA_USER@" in text
    assert "Group=@ATHENA_GROUP@" in text
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=@ATHENA_DATA_DIR@" in text
    assert "NoNewPrivileges=true" in text


def test_provenance_timer_invokes_the_fail_closed_checker():
    service = (SYSTEMD / "athena-provenance.service.in").read_text(encoding="utf-8")
    timer = (SYSTEMD / "athena-provenance.timer").read_text(encoding="utf-8")

    assert "scripts/check_runtime_provenance.py" in service
    assert "--checkout @ATHENA_CHECKOUT@" in service
    assert "--url @ATHENA_VERSION_URL@" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer
    assert "ProtectSystem=strict" in service
    assert "NoNewPrivileges=true" in service


def test_templates_do_not_capture_the_current_host_or_credentials():
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SYSTEMD.iterdir())
    )

    for forbidden in (
        "kevin-lee",
        "100.125.80.91",
        "ATHENA_TELEGRAM_TOKEN=",
        "ATHENA_SESSION_SECRET=",
        "ATHENA_OIDC_CLIENT_SECRET=",
    ):
        assert forbidden not in combined
