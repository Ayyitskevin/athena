"""The startup warning for insecure (non-Secure) session cookies.

Athena keeps ATHENA_COOKIE_SECURE off by default so http dev and the test suite
work. A cookie set on a request that arrives over HTTPS gets the Secure flag
anyway; behind a TLS-terminating proxy the request arrives as plain HTTP, so a
raw-factory deployment there that forgets the setting would ship the session and
CSRF cookies without the flag — capturable by a network attacker on a plain-http
side channel. There is no dev/prod signal to key off, so instead of a hard flip
we warn loudly, exactly once per process, when a raw-factory app starts serving.
(``athena-serve`` serves direct HTTP and refuses the setting; what its start
says is pinned in ``tests/test_startup_notices.py``.)
"""

import logging

from fastapi.testclient import TestClient

from athena import config, main
from athena.main import create_app


def _start(app) -> None:
    with TestClient(app):
        pass


def test_warns_when_cookies_are_not_secure(tmp_path, caplog, monkeypatch):
    # WHY: with COOKIE_SECURE off, starting a raw-factory app must emit one WARNING
    # naming the env var, so a deploy behind a TLS proxy doesn't silently ship
    # insecure cookies.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.WARNING, logger="athena"):
        _start(create_app(tmp_path / "warn.db"))

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "ATHENA_COOKIE_SECURE" in r.message
    ]
    assert len(warnings) == 1


def test_warning_fires_only_once_per_process(tmp_path, caplog, monkeypatch):
    # WHY: a test suite (or a multi-app process) serves many apps; the warning must
    # not spam. Once the flag is set, later starts stay silent.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.WARNING, logger="athena"):
        _start(create_app(tmp_path / "once_a.db"))
        _start(create_app(tmp_path / "once_b.db"))

    warnings = [r for r in caplog.records if "ATHENA_COOKIE_SECURE" in r.message]
    assert len(warnings) == 1


def test_no_warning_when_cookies_are_secure(tmp_path, caplog, monkeypatch):
    # WHY: the whole point is a correctly-configured HTTPS deploy (COOKIE_SECURE on)
    # is silent — no false alarm.
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    monkeypatch.setattr(main, "_cookie_secure_warned", False)

    with caplog.at_level(logging.WARNING, logger="athena"):
        _start(create_app(tmp_path / "secure.db"))

    assert not [r for r in caplog.records if "ATHENA_COOKIE_SECURE" in r.message]
