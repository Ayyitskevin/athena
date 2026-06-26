"""Outbound webhooks — registration, the SSRF guard, and cursor-based delivery.

The push twin of the pull event feed. These encode the contract that matters:

  * managing webhooks is admin-only, and the signing secret is shown exactly once;
  * Athena must not be tricked into POSTing to internal addresses (SSRF);
  * delivery is ordered, HMAC-signed, advances a per-webhook cursor, starts at the
    tip (no history replay), filters by kind, backs off a failing endpoint, and
    isolates one bad endpoint from the others.

Delivery is exercised through webhooks.deliver_pending with a STUB poster, so the
logic is tested without real network or background-loop timing (the live loop is
disabled in tests by conftest).
"""
from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient

from athena.core import activity, db, webhooks
from athena.main import create_app

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _conn(db_file, role="admin"):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('a@e.com', 'A', ?)", (role,)
    )
    conn.commit()
    return conn


def _event(conn, *, kind="issue", target_id=1, verb="created"):
    return activity.record(
        conn, actor_id=1, verb=verb, target_kind=kind, target_id=target_id
    )


class _Poster:
    """A stub poster recording every delivery and returning a fixed verdict."""

    def __init__(self, ok=True, error=None):
        self.ok, self.error, self.calls = ok, error, []

    def __call__(self, url, body, headers):
        self.calls.append({"url": url, "body": body, "headers": headers})
        return self.ok, self.error


# --- SSRF guard -------------------------------------------------------------


def test_is_safe_url_accepts_public_address():
    ok, _ = webhooks.is_safe_url("https://93.184.216.34/hook")
    assert ok is True


def test_is_safe_url_rejects_internal_and_bad_schemes():
    bad = [
        "http://127.0.0.1/x",          # loopback
        "http://10.0.0.5/x",           # private
        "http://192.168.1.10/x",       # private
        "http://169.254.169.254/x",    # link-local (cloud metadata)
        "http://[::1]/x",              # ipv6 loopback
        "http://localhost/x",          # resolves to loopback
        "ftp://93.184.216.34/x",       # wrong scheme
        "https:///nohost",             # no host
    ]
    for url in bad:
        ok, reason = webhooks.is_safe_url(url)
        assert ok is False, f"expected {url} to be rejected"
        assert reason


# --- signing ----------------------------------------------------------------


def test_signature_is_hmac_sha256_over_the_body():
    body = b'{"a":1}'
    assert webhooks.sign("topsecret", body).startswith("sha256=")
    # Different secret -> different signature; same inputs -> stable signature.
    assert webhooks.sign("a", body) != webhooks.sign("b", body)
    assert webhooks.sign("a", body) == webhooks.sign("a", body)


# --- CRUD API (admin only) --------------------------------------------------


def test_create_returns_secret_once_then_list_hides_it(tmp_path):
    app = create_app(tmp_path / "crud.db")
    with TestClient(app) as client:
        _conn(tmp_path / "crud.db").close()
        h = {"X-Athena-Actor": "1"}
        created = client.post(
            "/webhooks", json={"url": "https://93.184.216.34/hook"}, headers=h
        )
        assert created.status_code == 201
        body = created.json()
        assert body["secret"].startswith("whsec_")
        assert body["url"] == "https://93.184.216.34/hook"

        listed = client.get("/webhooks", headers=h).json()
        assert len(listed) == 1
        assert "secret" not in listed[0]  # never exposed again


def test_create_rejects_unsafe_url(tmp_path):
    app = create_app(tmp_path / "ssrf.db")
    with TestClient(app) as client:
        _conn(tmp_path / "ssrf.db").close()
        r = client.post(
            "/webhooks", json={"url": "http://169.254.169.254/latest/meta-data"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_webhook_management_is_admin_only(tmp_path):
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        conn = _conn(tmp_path / "auth.db")
        # A second, non-admin user.
        conn.execute(
            "INSERT INTO users (email, name, role) VALUES ('m@e.com', 'M', 'member')"
        )
        conn.commit()
        conn.close()
        url = {"url": "https://93.184.216.34/hook"}
        assert client.post("/webhooks", json=url).status_code == 401  # no actor
        assert (
            client.post("/webhooks", json=url, headers={"X-Athena-Actor": "2"}).status_code
            == 403  # member, not admin
        )
        assert (
            client.post("/webhooks", json=url, headers={"X-Athena-Actor": "1"}).status_code
            == 201  # admin
        )


def test_delete_then_404(tmp_path):
    app = create_app(tmp_path / "del.db")
    with TestClient(app) as client:
        _conn(tmp_path / "del.db").close()
        h = {"X-Athena-Actor": "1"}
        wid = client.post(
            "/webhooks", json={"url": "https://93.184.216.34/hook"}, headers=h
        ).json()["id"]
        assert client.delete(f"/webhooks/{wid}", headers=h).status_code == 204
        assert client.get(f"/webhooks/{wid}", headers=h).status_code == 404
        assert client.delete(f"/webhooks/{wid}", headers=h).status_code == 404


def test_new_webhook_starts_at_tip_no_history(tmp_path):
    # WHY: registering a webhook must not replay the whole backlog at the endpoint.
    app = create_app(tmp_path / "tip.db")
    with TestClient(app) as client:
        conn = _conn(tmp_path / "tip.db")
        _event(conn, target_id=1)
        _event(conn, target_id=2)  # two events exist BEFORE registration
        conn.close()
        wid = client.post(
            "/webhooks", json={"url": "https://93.184.216.34/hook"},
            headers={"X-Athena-Actor": "1"},
        ).json()["id"]
        # The cursor was set to the current tip, so nothing pre-registration is pending.
        conn = db.connect(tmp_path / "tip.db")
        poster = _Poster()
        assert webhooks.deliver_pending(conn, poster=poster, now=NOW) == 0
        # A new event after registration IS delivered.
        _event(conn, target_id=3)
        assert webhooks.deliver_pending(conn, poster=poster, now=NOW) == 1
        assert webhooks.get_webhook(conn, wid)["cursor"] > 0
        conn.close()


# --- delivery logic (stub poster) -------------------------------------------


def test_delivery_signs_orders_and_advances_cursor(tmp_path):
    conn = _conn(tmp_path / "d.db")
    wh = webhooks.create_webhook(
        conn, url="https://93.184.216.34/hook", created_by=1, start_cursor=0
    )
    e1 = _event(conn, target_id=10, verb="created")
    e2 = _event(conn, target_id=10, verb="changed_status")

    poster = _Poster(ok=True)
    delivered = webhooks.deliver_pending(conn, poster=poster, now=NOW)
    assert delivered == 2
    # In id order.
    assert [json.loads(c["body"])["id"] for c in poster.calls] == [e1["id"], e2["id"]]
    # Each carries a valid signature over its exact body, plus event headers.
    for c in poster.calls:
        assert c["headers"]["X-Athena-Signature"] == webhooks.sign(wh["secret"], c["body"])
        assert c["headers"]["Content-Type"] == "application/json"
    # Cursor advanced to the last delivered event; a re-run delivers nothing.
    assert webhooks.get_webhook(conn, wh["id"])["cursor"] == e2["id"]
    assert webhooks.deliver_pending(conn, poster=_Poster(), now=NOW) == 0
    conn.close()


def test_delivery_filters_by_event_kind(tmp_path):
    conn = _conn(tmp_path / "k.db")
    webhooks.create_webhook(
        conn, url="https://93.184.216.34/hook", created_by=1,
        event_kind="page", start_cursor=0,
    )
    _event(conn, kind="issue", target_id=1)
    page_ev = _event(conn, kind="page", target_id=2)
    _event(conn, kind="issue", target_id=3)

    poster = _Poster(ok=True)
    webhooks.deliver_pending(conn, poster=poster, now=NOW)
    kinds = [json.loads(c["body"])["target_kind"] for c in poster.calls]
    assert kinds == ["page"]
    assert json.loads(poster.calls[0]["body"])["id"] == page_ev["id"]
    conn.close()


def test_failure_records_error_holds_cursor_and_backs_off(tmp_path):
    conn = _conn(tmp_path / "f.db")
    wh = webhooks.create_webhook(
        conn, url="https://93.184.216.34/hook", created_by=1, start_cursor=0
    )
    _event(conn, target_id=1)

    failing = _Poster(ok=False, error="boom")
    assert webhooks.deliver_pending(conn, poster=failing, now=NOW) == 0
    row = webhooks.get_webhook(conn, wh["id"])
    assert row["failure_count"] == 1
    assert row["last_error"] == "boom"
    assert row["cursor"] == 0  # not advanced — the event will be retried
    assert row["next_attempt_at"] is not None  # backoff armed

    # Within the backoff window the webhook is skipped (poster not called).
    skip = _Poster(ok=True)
    assert webhooks.deliver_pending(conn, poster=skip, now=NOW) == 0
    assert skip.calls == []

    # After the backoff window it retries and, on success, recovers fully.
    later = datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    ok = _Poster(ok=True)
    assert webhooks.deliver_pending(conn, poster=ok, now=later) == 1
    recovered = webhooks.get_webhook(conn, wh["id"])
    assert recovered["failure_count"] == 0
    assert recovered["last_error"] is None
    assert recovered["cursor"] > 0
    conn.close()


def test_one_bad_endpoint_does_not_block_others(tmp_path):
    conn = _conn(tmp_path / "iso.db")
    bad = webhooks.create_webhook(
        conn, url="https://93.184.216.34/bad", created_by=1, start_cursor=0
    )
    good = webhooks.create_webhook(
        conn, url="https://93.184.216.34/good", created_by=1, start_cursor=0
    )
    _event(conn, target_id=1)

    # The bad endpoint fails; the good one still gets the event. (Same poster:
    # fail for /bad, succeed for /good, keyed on the URL.)
    def poster(url, body, headers):
        return ("/good" in url), (None if "/good" in url else "down")

    webhooks.deliver_pending(conn, poster=poster, now=NOW)
    assert webhooks.get_webhook(conn, good["id"])["cursor"] > 0
    assert webhooks.get_webhook(conn, bad["id"])["cursor"] == 0
    assert webhooks.get_webhook(conn, bad["id"])["failure_count"] == 1
    conn.close()


def test_delivery_skips_unsafe_url_without_posting(tmp_path):
    # A webhook whose URL resolves internally (inserted directly) must be refused at
    # delivery time too, not just registration — DNS could have been re-pointed.
    conn = _conn(tmp_path / "u.db")
    wh = webhooks.create_webhook(
        conn, url="http://127.0.0.1/hook", created_by=1, start_cursor=0
    )
    _event(conn, target_id=1)
    poster = _Poster(ok=True)
    assert webhooks.deliver_pending(conn, poster=poster, now=NOW) == 0
    assert poster.calls == []
    assert "unsafe url" in webhooks.get_webhook(conn, wh["id"])["last_error"]
    conn.close()


def test_run_delivery_pass_executes_against_a_real_db(tmp_path):
    # Exercises the live wrapper (db.connect + deliver_pending + real urllib poster)
    # end to end, but with an internal URL so the SSRF guard short-circuits before
    # any network call — no real request leaves the test.
    db_file = tmp_path / "live.db"
    conn = _conn(db_file)
    webhooks.create_webhook(
        conn, url="http://127.0.0.1/hook", created_by=1, start_cursor=0
    )
    _event(conn, target_id=1)
    conn.close()
    assert webhooks.run_delivery_pass(db_file) == 0
