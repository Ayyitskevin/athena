"""The startup warning for insecure (non-Secure) session cookies.

Athena keeps ATHENA_COOKIE_SECURE off by default so http dev and the test suite
work, but a real HTTPS deploy that forgets to flip it would ship the session and
CSRF cookies without the Secure flag — capturable by a network attacker on a
plain-http side channel. There is no dev/prod signal to key off, so instead of a
hard flip we warn loudly, exactly once per process, at app construction time.
"""

import logging

from athena import config, main
from athena.main import create_app


def test_warns_when_cookies_are_not_secure(tmp_path, caplog, monkeypatch):
    # WHY: with COOKIE_SECURE off, building the app must emit one WARNING naming the
    # env var to set, so an HTTPS deploy doesn't silently ship insecure cookies.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.WARNING, logger="athena"):
        create_app(tmp_path / "warn.db")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ATHENA_COOKIE_SECURE" in warnings[0].message


def test_warning_fires_only_once_per_process(tmp_path, caplog, monkeypatch):
    # WHY: a test suite (or a multi-app process) builds create_app() many times; the
    # warning must not spam. Once the flag is set, later builds stay silent.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.WARNING, logger="athena"):
        create_app(tmp_path / "once_a.db")
        create_app(tmp_path / "once_b.db")

    warnings = [r for r in caplog.records if "ATHENA_COOKIE_SECURE" in r.message]
    assert len(warnings) == 1


def test_no_warning_when_cookies_are_secure(tmp_path, caplog, monkeypatch):
    # WHY: the whole point is a correctly-configured HTTPS deploy (COOKIE_SECURE on)
    # is silent — no false alarm.
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.WARNING, logger="athena"):
        create_app(tmp_path / "secure.db")

    assert not [r for r in caplog.records if "ATHENA_COOKIE_SECURE" in r.message]
