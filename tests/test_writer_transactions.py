"""Every writer composes inside its caller's transaction (G2).

WHY: a writer that commits whatever it is asked cannot be part of a command.
Called inside another's ``db.transaction`` it commits that transaction's first
half, so a command that then fails has already landed part of its work, and
the block raises ``TransactionEndedEarly``. Each writer here once ended in
``conn.commit()`` or opened ``BEGIN IMMEDIATE`` by hand; each now writes
through ``db.transaction``, which is a savepoint inside a caller's transaction.

Two things are pinned for each, and both matter. Inside a caller's
transaction that then fails, the write rolls back with it. Called on its own,
it still commits: another connection sees it, so nothing that relied on the
old commit lost its write.

``scripts/check_transaction_commits.py`` keeps new ones from appearing; these
tests show the converted ones behave.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from athena.aegis import automation, projects, sprints
from athena.core import (
    activity,
    agent_signal_commands,
    attachments,
    db,
    maintenance_commands,
    notifications,
    oidc,
    passwords,
    sessions,
    tokens,
    users,
    webhooks,
)
from athena.mentor import pages, spaces

ADMIN, AGENT, WATCHER = 1, 2, 3


@dataclass
class _Env:
    path: Path
    monkeypatch: pytest.MonkeyPatch
    ctx: dict[str, Any]


@dataclass(frozen=True)
class _Case:
    name: str
    write: Callable[[sqlite3.Connection, _Env], object]
    state: Callable[[sqlite3.Connection, _Env], object]
    setup: Callable[[sqlite3.Connection, _Env], None] = lambda conn, env: None


def _one(conn: sqlite3.Connection, sql: str, *params: object) -> object:
    row = conn.execute(sql, params).fetchone()
    return tuple(row) if row is not None else None


# --- setups (each commits its own fixture rows) -------------------------------


def _watch(conn, env):
    notifications.watch(conn, WATCHER, "issue", 7, commit=True)


def _notified(conn, env):
    _watch(conn, env)
    activity.record(
        conn,
        actor_id=ADMIN,
        verb="commented",
        target_kind="issue",
        target_id=7,
        commit=True,
    )
    env.ctx["notification"] = conn.execute("SELECT id FROM notifications").fetchone()[0]


def _preference(conn, env):
    _watch(conn, env)
    notifications.set_preference(conn, WATCHER, "issue", 7, priority="high")


def _session(conn, env):
    env.ctx["raw"] = sessions.create_session(conn, ADMIN)


def _login_state(conn, env):
    oidc.start_login(conn, state="s", nonce="n", code_verifier="v")


def _legacy_hash(conn, env):
    env.monkeypatch.setattr(passwords, "_ITERATIONS", 1_000)
    users.create_user(conn, email="old@e.com", name="Old", password="pw", commit=True)
    env.monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)


def _stored(conn, env):
    env.ctx["attachment"] = attachments.store(
        conn,
        target_kind="issue",
        target_id=7,
        filename="a.txt",
        content_type="text/plain",
        data=b"hello",
        uploaded_by=ADMIN,
        attach_dir=env.path.parent / "blobs",
    )


def _webhook(conn, env):
    env.ctx["webhook"] = webhooks.create_webhook(
        conn, url="https://hooks.example.com/x", created_by=ADMIN, commit=True
    )["id"]


def _expired_session(conn, env):
    conn.execute(
        "INSERT INTO sessions (user_id, session_hash, expires_at, csrf_token) "
        "VALUES (?, 'h', '2000-01-01 00:00:00', 'c')",
        (ADMIN,),
    )
    conn.commit()


def _token(conn, env):
    env.ctx["token"] = tokens.create_token(
        conn, user_id=AGENT, name="t", scopes=["read"], commit=True
    )["id"]


def _sprint(conn, env):
    project = projects.create_project(
        conn, name="P", key="P", created_by=ADMIN, commit=True
    )
    env.ctx["sprint"] = sprints.create_sprint(
        conn, project_id=project["id"], name="S1", commit=True
    )["id"]


def _pages(conn, env):
    space = spaces.create_space(
        conn, key="ENG", name="Eng", created_by=ADMIN, commit=True
    )
    for name in ("first", "second"):
        env.ctx[name] = pages.create_page(
            conn, space_id=space["id"], title=name, created_by=ADMIN, commit=True
        )


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

CASES = [
    _Case(
        "notifications.unwatch",
        setup=_watch,
        write=lambda c, e: notifications.unwatch(c, WATCHER, "issue", 7),
        state=lambda c, e: notifications.is_watching(c, WATCHER, "issue", 7),
    ),
    _Case(
        "notifications.mark_read",
        setup=_notified,
        write=lambda c, e: notifications.mark_read(c, WATCHER, e.ctx["notification"]),
        state=lambda c, e: _one(c, "SELECT read_at FROM notifications"),
    ),
    _Case(
        "notifications.mark_all_read",
        setup=_notified,
        write=lambda c, e: notifications.mark_all_read(c, WATCHER),
        state=lambda c, e: _one(c, "SELECT read_at FROM notifications"),
    ),
    _Case(
        "notifications.set_preference",
        setup=_watch,
        write=lambda c, e: notifications.set_preference(
            c, WATCHER, "issue", 7, priority="urgent"
        ),
        state=lambda c, e: _one(c, "SELECT priority FROM watch_preferences"),
    ),
    _Case(
        "notifications.delete_preference",
        setup=_preference,
        write=lambda c, e: notifications.delete_preference(c, WATCHER, "issue", 7),
        state=lambda c, e: _one(c, "SELECT priority FROM watch_preferences"),
    ),
    _Case(
        "sessions.create_session",
        write=lambda c, e: sessions.create_session(c, ADMIN),
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM sessions"),
    ),
    _Case(
        "sessions.destroy_session",
        setup=_session,
        write=lambda c, e: sessions.destroy_session(c, e.ctx["raw"]),
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM sessions"),
    ),
    _Case(
        "oidc.start_login",
        write=lambda c, e: oidc.start_login(c, state="s", nonce="n", code_verifier="v"),
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM oidc_login_states"),
    ),
    _Case(
        "oidc.take_login_state",
        setup=_login_state,
        write=lambda c, e: oidc.take_login_state(c, "s"),
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM oidc_login_states"),
    ),
    _Case(
        "users.verify_credentials (rehash)",
        setup=_legacy_hash,
        write=lambda c, e: users.verify_credentials(
            c, email="old@e.com", password="pw"
        ),
        state=lambda c, e: _one(
            c, "SELECT password_hash FROM users WHERE email = 'old@e.com'"
        ),
    ),
    _Case(
        "attachments.store",
        write=_stored,
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM attachments"),
    ),
    _Case(
        "attachments.delete",
        setup=_stored,
        write=lambda c, e: attachments.delete(
            c, e.ctx["attachment"]["id"], e.path.parent / "blobs"
        ),
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM attachments"),
    ),
    _Case(
        "automation._set_cursor",
        write=lambda c, e: automation._set_cursor(c, 42),
        state=lambda c, e: automation.get_cursor(c),
    ),
    _Case(
        "automation._set_schedule_scan_cursor",
        write=lambda c, e: automation._set_schedule_scan_cursor(c, 42),
        state=lambda c, e: automation._get_schedule_scan_cursor(c),
    ),
    _Case(
        "webhooks._record_success",
        setup=_webhook,
        write=lambda c, e: webhooks._record_success(c, e.ctx["webhook"], 5, _NOW),
        state=lambda c, e: _one(c, "SELECT cursor, last_success_at FROM webhooks"),
    ),
    _Case(
        "webhooks._record_failure",
        setup=_webhook,
        write=lambda c, e: webhooks._record_failure(
            c, e.ctx["webhook"], 0, "refused", _NOW
        ),
        state=lambda c, e: _one(c, "SELECT failure_count, last_error FROM webhooks"),
    ),
    _Case(
        "maintenance_commands.prune",
        setup=_expired_session,
        write=lambda c, e: maintenance_commands.prune(c),
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM sessions"),
    ),
    _Case(
        "agent_signal_commands.record_refusal",
        write=lambda c, e: agent_signal_commands.record_refusal(
            c, user_id=AGENT, code="not_found", status_code=404, method="GET", path="/x"
        ),
        state=lambda c, e: _one(c, "SELECT last_refusal_code FROM agent_last_signals"),
    ),
    _Case(
        "tokens._stamp_last_use",
        setup=_token,
        write=lambda c, e: tokens._stamp_last_use(c, e.ctx["token"]),
        state=lambda c, e: _one(c, "SELECT last_used_at FROM api_tokens"),
    ),
    _Case(
        "sprints.start_sprint(commit=True)",
        setup=_sprint,
        write=lambda c, e: sprints.start_sprint(c, e.ctx["sprint"], commit=True),
        state=lambda c, e: _one(c, "SELECT state FROM sprints"),
    ),
    _Case(
        "pages.update_page(commit=True)",
        setup=_pages,
        write=lambda c, e: pages.update_page(
            c, e.ctx["first"]["id"], editor_id=ADMIN, body="v2", commit=True
        ),
        state=lambda c, e: (
            _one(c, "SELECT body FROM pages WHERE title = 'first'"),
            _one(c, "SELECT COUNT(*) FROM page_versions"),
        ),
    ),
    _Case(
        "pages.move(commit=True)",
        setup=_pages,
        write=lambda c, e: pages.move(
            c, e.ctx["second"], e.ctx["first"]["id"], commit=True
        ),
        state=lambda c, e: _one(
            c, "SELECT parent_id FROM pages WHERE title = 'second'"
        ),
    ),
    _Case(
        "pages.delete_page",
        setup=_pages,
        write=lambda c, e: pages.delete_page(c, e.ctx["second"]["id"]),
        state=lambda c, e: _one(c, "SELECT COUNT(*) FROM pages"),
    ),
]


class _Abort(Exception):
    """The caller's own work fails after the writer returned."""


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "writers.db"
    conn = db.connect(path)
    db.migrate(conn)
    for email, is_agent in (("a@e.com", 0), ("bot@e.com", 1), ("w@e.com", 0)):
        conn.execute(
            "INSERT INTO users (email, name, role, is_agent) VALUES (?, ?, ?, ?)",
            (email, email, "admin" if email == "a@e.com" else "member", is_agent),
        )
    conn.commit()
    conn.close()
    return _Env(path, monkeypatch, {})


def _setup(case: _Case, env: _Env) -> sqlite3.Connection:
    conn = db.connect(env.path)
    case.setup(conn, env)
    assert not conn.in_transaction, "a setup left its fixture uncommitted"
    return conn


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_writer_rolls_back_with_the_transaction_it_was_called_in(env, case):
    conn = _setup(case, env)
    before = case.state(conn, env)

    with pytest.raises(_Abort):
        with db.transaction(conn):
            case.write(conn, env)
            assert case.state(conn, env) != before, "the writer wrote nothing"
            raise _Abort

    # Not TransactionEndedEarly: nothing inside committed, and the write went
    # back with its caller.
    assert case.state(conn, env) == before
    conn.close()


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_writer_called_on_its_own_still_commits(env, case):
    conn = _setup(case, env)
    before = case.state(conn, env)

    case.write(conn, env)

    assert not conn.in_transaction
    other = db.connect(env.path)
    try:
        assert case.state(other, env) != before
    finally:
        other.close()
        conn.close()
