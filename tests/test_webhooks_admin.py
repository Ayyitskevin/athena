"""Operating webhooks: pause/resume (REST + web) and the admin UI.

The webhook substrate shipped without a way to actually SET `active` (a flaky
endpoint could only be deleted, losing its cursor) and without any web surface.
These tests pin the gap that closed: pausing stops delivery and resuming restores
it WITHOUT losing the cursor; resuming also clears a stale backoff so a fixed
endpoint retries promptly; the PATCH endpoint is admin-only; and the admin page
registers (secret shown once), toggles, and deletes.
"""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from athena.core import activity, db, webhooks
from athena.main import create_app

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
H1 = {"X-Athena-Actor": "1"}
HOOK = "https://93.184.216.34/hook"


class _Poster:
    def __init__(self, ok=True, error=None):
        self.ok, self.error, self.calls = ok, error, []

    def __call__(self, url, body, headers):
        self.calls.append(url)
        return self.ok, self.error


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name, role) VALUES ('a@e.com', 'A', 'admin')")
    conn.commit()
    return conn


def _login(client, email="a@e.com", password="pw"):
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- data layer + delivery --------------------------------------------------


def test_pause_stops_delivery_and_resume_restores_it(tmp_path):
    conn = _conn(tmp_path / "toggle.db")
    wid = webhooks.create_webhook(conn, url=HOOK, created_by=1, start_cursor=0)["id"]
    activity.record(conn, actor_id=1, verb="created", target_kind="issue", target_id=1)

    # Paused → the delivery pass skips it entirely (cursor untouched).
    assert webhooks.set_webhook_active(conn, wid, False)["active"] == 0
    assert webhooks.deliver_pending(conn, poster=_Poster(), now=NOW) == 0
    assert webhooks.get_webhook(conn, wid)["cursor"] == 0  # nothing lost

    # Resumed → it delivers the event it was holding at.
    assert webhooks.set_webhook_active(conn, wid, True)["active"] == 1
    assert webhooks.deliver_pending(conn, poster=_Poster(), now=NOW) == 1
    conn.close()


def test_resume_clears_a_stale_backoff(tmp_path):
    conn = _conn(tmp_path / "backoff.db")
    wid = webhooks.create_webhook(conn, url=HOOK, created_by=1, start_cursor=0)["id"]
    activity.record(conn, actor_id=1, verb="created", target_kind="issue", target_id=1)

    # A failure arms backoff: still active, but gated until next_attempt_at.
    assert webhooks.deliver_pending(conn, poster=_Poster(ok=False, error="boom"), now=NOW) == 0
    failed = webhooks.get_webhook(conn, wid)
    assert failed["failure_count"] == 1 and failed["next_attempt_at"] is not None
    # At the same instant it stays gated — nothing delivers yet.
    assert webhooks.deliver_pending(conn, poster=_Poster(), now=NOW) == 0

    # Resuming (operator fixed the receiver) clears the gate, so it retries now.
    cleared = webhooks.set_webhook_active(conn, wid, True)
    assert cleared["failure_count"] == 0
    assert cleared["last_error"] is None
    assert cleared["next_attempt_at"] is None
    assert webhooks.deliver_pending(conn, poster=_Poster(), now=NOW) == 1
    conn.close()


def test_set_active_unknown_webhook_is_none(tmp_path):
    conn = _conn(tmp_path / "missing.db")
    assert webhooks.set_webhook_active(conn, 999, True) is None
    conn.close()


# --- REST API ---------------------------------------------------------------


def test_patch_toggles_active(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        wid = client.post("/webhooks", json={"url": HOOK}, headers=H1).json()["id"]

        paused = client.patch(f"/webhooks/{wid}", json={"active": False}, headers=H1)
        assert paused.status_code == 200 and paused.json()["active"] == 0
        resumed = client.patch(f"/webhooks/{wid}", json={"active": True}, headers=H1)
        assert resumed.json()["active"] == 1
        assert client.patch("/webhooks/999", json={"active": False}, headers=H1).status_code == 404


def test_patch_is_admin_only(tmp_path):
    with TestClient(create_app(tmp_path / "guard.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        member = client.post(
            "/users", json={"email": "m@e.com", "name": "M", "role": "member"}, headers=H1
        ).json()
        wid = client.post("/webhooks", json={"url": HOOK}, headers=H1).json()["id"]
        denied = client.patch(
            f"/webhooks/{wid}",
            json={"active": False},
            headers={"X-Athena-Actor": str(member["id"])},
        )
        assert denied.status_code == 403


# --- admin web UI -----------------------------------------------------------


def test_admin_webhooks_register_toggle_delete(tmp_path):
    db_file = tmp_path / "web.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)

        page = client.get("/admin/webhooks")
        assert page.status_code == 200 and "Register webhook" in page.text

        # Register → the signing secret is shown exactly once.
        created = client.post(
            "/admin/webhooks", data={"url": HOOK, "event_kind": ""}, follow_redirects=False
        )
        assert created.status_code == 201
        assert "token-secret" in created.text and "shown once" in created.text

        wid = _only_webhook_id(db_file)
        # Pause from the UI.
        toggled = client.post(
            f"/admin/webhooks/{wid}/active", data={"active": "0"}, follow_redirects=False
        )
        assert toggled.status_code == 303
        assert _webhook_active(db_file, wid) == 0

        # Delete from the UI.
        client.post(f"/admin/webhooks/{wid}/delete", follow_redirects=False)
        assert _webhook_count(db_file) == 0


def test_admin_webhooks_requires_admin(tmp_path):
    with TestClient(create_app(tmp_path / "g.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw", "role": "member"},
            headers=H1,
        )
        _login(client, "m@e.com", "pw")
        denied = client.get("/admin/webhooks")
        assert denied.status_code == 403
        assert "Admin role required" in denied.text


def test_web_register_reapplies_the_ssrf_guard(tmp_path):
    # WHY: the web form re-implements the SSRF check inline, separate from the REST
    # boundary — so it needs its own test. A private/loopback URL must be refused
    # WITHOUT minting a secret or persisting a row (an admin can still be phished
    # into pointing Athena at cloud metadata / an internal host).
    db_file = tmp_path / "ssrf.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)
        for bad in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data"):
            resp = client.post(
                "/admin/webhooks", data={"url": bad, "event_kind": ""}, follow_redirects=False
            )
            assert resp.status_code == 400, bad
            assert "token-secret" not in resp.text  # no secret minted
        assert _webhook_count(db_file) == 0  # nothing persisted


def test_member_cannot_toggle_or_delete_webhook(tmp_path):
    # WHY: the destructive web routes are admin-gated only by an in-handler check
    # (verify_csrf runs first but checks no role). A logged-in member with a valid
    # CSRF token must still be refused — and the webhook left untouched.
    db_file = tmp_path / "memguard.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw", "role": "member"},
            headers=H1,
        )
        wid = client.post("/webhooks", json={"url": HOOK}, headers=H1).json()["id"]

        _login(client, "m@e.com", "pw")  # a valid member session + CSRF token
        toggle = client.post(
            f"/admin/webhooks/{wid}/active", data={"active": "0"}, follow_redirects=False
        )
        assert toggle.status_code == 403
        delete = client.post(f"/admin/webhooks/{wid}/delete", follow_redirects=False)
        assert delete.status_code == 403
        # The webhook is untouched: still active, still present.
        assert _webhook_active(db_file, wid) == 1
        assert _webhook_count(db_file) == 1


def test_csrf_required_on_webhook_web_routes(tmp_path):
    # WHY: the _login helper auto-supplies the CSRF token, so the happy-path tests
    # never prove the guard is wired. Strip the token and every mutating route must
    # refuse (403) inside the authenticated session — pinning verify_csrf onto each.
    db_file = tmp_path / "csrf.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        wid = client.post("/webhooks", json={"url": HOOK}, headers=H1).json()["id"]
        _login(client)
        client.headers.pop("X-CSRF-Token", None)  # drop the token the helper set

        assert client.post(
            "/admin/webhooks", data={"url": HOOK}, follow_redirects=False
        ).status_code == 403
        assert client.post(
            f"/admin/webhooks/{wid}/active", data={"active": "0"}, follow_redirects=False
        ).status_code == 403
        assert client.post(
            f"/admin/webhooks/{wid}/delete", follow_redirects=False
        ).status_code == 403
        # Nothing changed: exactly the one REST-created webhook, still active.
        assert _webhook_count(db_file) == 1
        assert _webhook_active(db_file, wid) == 1


def _only_webhook_id(db_file):
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT id FROM webhooks").fetchone()["id"]
    finally:
        conn.close()


def _webhook_active(db_file, wid):
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT active FROM webhooks WHERE id = ?", (wid,)).fetchone()["active"]
    finally:
        conn.close()


def _webhook_count(db_file):
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM webhooks").fetchone()["n"]
    finally:
        conn.close()
