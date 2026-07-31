"""Configuration values fail closed before the application starts."""

import os
import subprocess
import sys

import pytest


def _import_config(
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("ATHENA_")
    }
    env.update(overrides or {})
    return subprocess.run(
        [sys.executable, "-c", "import athena.config"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_configuration_imports_cleanly():
    result = _import_config()
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ATHENA_COOKIE_SECURE", "false"),
        ("ATHENA_TRUST_ACTOR_HEADER", "0"),
        ("ATHENA_WEBHOOK_DELIVERY", "off"),
        ("ATHENA_AUTOMATION", "no"),
    ],
)
def test_explicit_false_boolean_values_are_accepted(name, value):
    result = _import_config({name: value})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "ATHENA_COOKIE_SECURE",
        "ATHENA_TRUST_ACTOR_HEADER",
        "ATHENA_WEBHOOK_DELIVERY",
        "ATHENA_AUTOMATION",
    ],
)
def test_boolean_typos_are_rejected(name):
    result = _import_config({name: "truthy-ish"})
    assert result.returncode != 0
    assert f"{name} must be an explicit true/false value" in result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ATHENA_MAX_REQUEST_BODY_BYTES", "-1"),
        ("ATHENA_IDEMPOTENCY_TTL_SECONDS", "0"),
        ("ATHENA_TOKEN_RATE_LIMIT_PER_MINUTE", "-1"),
        ("ATHENA_AGENT_RUN_STALE_SECONDS", "0"),
        ("ATHENA_SESSION_TTL_DAYS", "0"),
        ("ATHENA_WEBHOOK_INTERVAL", "0"),
        ("ATHENA_WEBHOOK_TIMEOUT", "nan"),
        ("ATHENA_AUTOMATION_INTERVAL", "inf"),
        ("ATHENA_ATTACH_MAX_BYTES", "0"),
    ],
)
def test_out_of_range_numeric_values_are_rejected(name, value):
    result = _import_config({name: value})
    assert result.returncode != 0
    assert name in result.stderr


def test_non_integer_value_is_rejected():
    result = _import_config({"ATHENA_LOGIN_RATE_LIMIT_PER_MINUTE": "1.5"})
    assert result.returncode != 0
    assert "ATHENA_LOGIN_RATE_LIMIT_PER_MINUTE must be an integer" in result.stderr


def test_invalid_log_level_is_rejected():
    result = _import_config({"ATHENA_LOG_LEVEL": "verbose"})
    assert result.returncode != 0
    assert "ATHENA_LOG_LEVEL must be one of" in result.stderr


def test_valid_bootstrap_token_is_accepted():
    result = _import_config({"ATHENA_BOOTSTRAP_TOKEN": "x" * 32})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "value",
    [
        "x" * 31,
        "x" * 256,
        "has whitespace despite being much longer than thirty-two",
        "é" * 32,
    ],
)
def test_malformed_bootstrap_token_is_rejected_without_echoing_it(value):
    result = _import_config({"ATHENA_BOOTSTRAP_TOKEN": value})
    assert result.returncode != 0
    assert (
        "ATHENA_BOOTSTRAP_TOKEN must be 32-255 visible ASCII characters"
        in result.stderr
    )
    assert value not in result.stderr


def test_partial_oidc_configuration_is_rejected_without_echoing_values():
    issuer = "https://private-idp.example.invalid"
    result = _import_config({"ATHENA_OIDC_ISSUER": issuer})
    assert result.returncode != 0
    assert "OIDC configuration is partial" in result.stderr
    assert "ATHENA_OIDC_CLIENT_SECRET" in result.stderr
    assert issuer not in result.stderr


def test_complete_oidc_configuration_is_accepted():
    result = _import_config(
        {
            "ATHENA_OIDC_ISSUER": "https://idp.example.invalid",
            "ATHENA_OIDC_CLIENT_ID": "client",
            "ATHENA_OIDC_CLIENT_SECRET": "secret",
            "ATHENA_OIDC_REDIRECT_URL": "https://app.example.invalid/callback",
        }
    )
    assert result.returncode == 0, result.stderr
