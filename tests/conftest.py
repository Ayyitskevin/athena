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
from athena.core import db, passwords


@pytest.fixture(autouse=True)
def trust_actor_header_enabled(monkeypatch):
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)


@pytest.fixture(autouse=True)
def webhook_delivery_disabled(monkeypatch):
    # The webhook delivery loop makes real outbound HTTP requests on a timer. Tests
    # must not spawn it (it would fire against whatever URL a test registers); they
    # drive webhooks.deliver_pending directly with a stub poster instead. A test that
    # specifically wants the live loop can flip this back on.
    monkeypatch.setattr(config, "WEBHOOK_DELIVERY_ENABLED", False)


@pytest.fixture(autouse=True)
def automation_loop_disabled(monkeypatch):
    # The automation engine runs a background loop that fires rule actions on a timer.
    # Tests must not spawn it (it would act on issues mid-test, non-deterministically);
    # they drive automation.run_pass / process_pending directly. Flip back on for a test
    # that specifically wants the live loop.
    monkeypatch.setattr(config, "AUTOMATION_ENABLED", False)


@pytest.fixture(autouse=True)
def isolated_attachment_dir(tmp_path, monkeypatch):
    # Attachments write blobs to config.ATTACH_DIR; point it at a per-test temp dir
    # so uploads never land in the repo and tests can't see each other's files.
    monkeypatch.setattr(config, "ATTACH_DIR", tmp_path / "attachments")


@pytest.fixture
def migration_inventory_through(tmp_path, monkeypatch):
    """Expose one real, contiguous historical migration inventory to a test."""
    source = db.MIGRATIONS_DIR

    def limit(last_version: str):
        destination = tmp_path / "migration-inventory"
        destination.mkdir()
        for migration in sorted(source.glob("*.sql")):
            if migration.name <= last_version:
                (destination / migration.name).write_bytes(migration.read_bytes())
        monkeypatch.setattr(db, "MIGRATIONS_DIR", destination)
        return destination

    return limit


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    # Production PBKDF2 cost is deliberately expensive (600k iterations ≈ 200ms);
    # hundreds of tests create users and log in, so run the suite at a cheap cost.
    # The hash FORMAT and verify/rehash logic are identical at any count, and the
    # policy itself (600k, needs_rehash, dummy_verify cost parity) is covered by
    # dedicated tests that pin explicit iteration values.
    monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)
