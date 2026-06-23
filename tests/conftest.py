"""Shared test configuration.

The production default for the X-Athena-Actor header is now OFF (see
`athena.config`): an unconfigured, network-exposed instance must not trust a
spoofable identity header. The existing suite, however, models the *trusted
local box* mode where that header IS the identity — almost every test
authenticates by sending `X-Athena-Actor`. So we re-enable it by default here.

Tests that specifically exercise the locked-down default flip it back off inside
the test (e.g. `monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)`).
`athena.core.identity` reads the flag at call time, so a per-test override takes
effect immediately.
"""
import pytest

from athena import config


@pytest.fixture(autouse=True)
def trust_actor_header_enabled(monkeypatch):
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
