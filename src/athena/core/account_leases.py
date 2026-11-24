"""An account's active leases, as the acts that stop an account name and release
them (requirement C8, decision D12).

Pausing an account, revoking its tokens, offboarding it and removing it stop
the account, but every lease it holds keeps its issue and its path fence until
the lease expires, and a stopped account cannot release them. So each of those
acts names the account's active leases in its answer, and the operator may
release them in the same act by asking (``release_leases``). Releasing is an
explicit option, never a default: ruling K1 stands, and nothing is swept.

Each release goes through the lease command an admin's release of one lease
uses (``aegis/lease_commands.py``, U3), inside the act's own transaction, so it
is recorded per lease as ``lease_released`` naming the holder, and the act and
its releases commit or roll back together.

``core`` owns the acts and may not import ``aegis`` (the import contract), so
the composition root registers the lease side, as it registers the feed's
observers and undo's inverses. Registration is idempotent: every test builds
its own app in one process.
"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3

#: The lister: the holder's active leases on issues ``actor`` may see, each as
#: ``{issue_id, key, title, generation, expires_at}``. A lease on an issue the
#: actor cannot see is left out: a hidden thing reads as missing.
Lister = Callable[[sqlite3.Connection, dict, int], list[dict]]
#: The releaser: end one listed lease as ``actor``, inside the caller's
#: transaction. It raises if the lease cannot be released.
Releaser = Callable[[sqlite3.Connection, dict, dict], object]

_REGISTERED: dict[str, Callable] = {}


def register(lister: Lister, releaser: Releaser) -> None:
    """Install the lease side. Re-registering the same pair is a no-op; a
    different one is a bug, and raises."""
    for name, function in (("list", lister), ("release", releaser)):
        existing = _REGISTERED.get(name)
        if existing is not None and existing is not function:
            raise ValueError(f"account lease {name!r} is already registered")
    _REGISTERED["list"] = lister
    _REGISTERED["release"] = releaser


def _registered(name: str) -> Callable:
    function = _REGISTERED.get(name)
    if function is None:
        raise RuntimeError(
            "no account lease steward is registered; build the app with create_app"
        )
    return function


def _release(conn: sqlite3.Connection, actor: dict, lease: dict) -> None:
    _registered("release")(conn, actor, lease)


def name_and_release(
    conn: sqlite3.Connection, *, actor: dict, holder_id: int, release: bool
) -> list[dict]:
    """The holder's active leases ``actor`` may see, each with ``released``,
    after releasing every one of them when ``release`` is true.

    Call it inside the act's write transaction, after the act's own change:
    the list and the releases then describe the same instant, and a failed
    release rolls the act back with it."""
    listed = _registered("list")(conn, actor, holder_id)
    for lease in listed:
        if release:
            _release(conn, actor, lease)
        lease["released"] = release
    return listed
