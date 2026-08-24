"""athena-seat-doctor proves a seat's wiring, or fails at the first broken link.

Why these boundaries matter: the seat doctor exists so "is this agent wired?"
is a command, not an investigation (docs/AGENT_BOOT.md's wire-first rule). A
doctor that greenlights an unusable seat is worse than none — so every test
here pins a way the wiring can be broken while still *looking* plausible: a
paused account whose token still authenticates, a session that authenticates
without being a revocable bearer token, a token whose scopes silently widened
or narrowed, a dead credential.
"""

from fastapi.testclient import TestClient

from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError
from athena.ops import _seat_doctor_checks, seat_doctor_main

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path):
    return create_app(tmp_path / "seat.db")


def _onboarded_seat(client):
    """Bootstrap an admin, onboard one agent, return its raw bearer token."""
    client.post("/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"})
    response = client.post(
        "/users/onboard_agent",
        json={"email": "sol@e.com", "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer_client(app, raw_token):
    transport = TestClient(app)
    transport.headers.update({"Authorization": f"Bearer {raw_token}"})
    return AthenaClient(client=transport)


def test_wired_seat_passes_all_checks(tmp_path):
    """The pass path: server, identity, scopes, limits, desk — five ok lines,
    with the desk counts coming from the real bounded envelopes."""
    app = _app(tmp_path)
    with TestClient(app) as c:
        seat = _onboarded_seat(c)
        client = _bearer_client(app, seat["token"]["token"])
        checks = _seat_doctor_checks(
            client, expect_scopes=frozenset({"read", "issue:write"})
        )
    assert [line.split(":")[0] for line in checks] == [
        "server",
        "identity",
        "scopes",
        "limits",
        "desk",
    ]
    assert "issue:write" in checks[2]
    assert "delegations=0" in checks[4] and "leases held=0" in checks[4]


def test_paused_seat_fails_even_though_its_token_authenticates(tmp_path):
    """Pausing an agent does not revoke its token — whoami still answers. The
    doctor must read paused_at as broken wiring, not report a green seat that
    will 403 on its first write."""
    app = _app(tmp_path)
    with TestClient(app) as c:
        seat = _onboarded_seat(c)
        paused = c.put(
            f"/users/{seat['user']['id']}/paused", json={"paused": True}, headers=H1
        )
        assert paused.status_code == 200, paused.text
        client = _bearer_client(app, seat["token"]["token"])
        try:
            _seat_doctor_checks(client, expect_scopes=None)
            raise AssertionError("expected the paused seat to fail")
        except AthenaError as exc:
            # The server itself refuses a paused account at whoami — the
            # doctor's job is to surface that refusal, reason intact.
            assert exc.status_code == 403
            assert "paused" in str(exc)


def test_scope_drift_fails_in_both_directions(tmp_path):
    """--expect-scopes is an exact-set assertion: narrower silently breaks the
    seat, wider is authority nobody reviewed. Either way the doctor refuses."""
    app = _app(tmp_path)
    with TestClient(app) as c:
        seat = _onboarded_seat(c)
        client = _bearer_client(app, seat["token"]["token"])
        try:
            _seat_doctor_checks(client, expect_scopes=frozenset({"read"}))
            raise AssertionError("expected the widened token to fail")
        except ValueError as exc:
            message = str(exc)
            assert "issue:write" in message and "expected" in message


def test_session_auth_is_not_a_seat(tmp_path):
    """The trusted actor-header path authenticates but is not token-scoped —
    it cannot be rotated, revoked, or budgeted per agent, so the doctor must
    refuse it rather than bless wiring no operator can later cut."""
    app = _app(tmp_path)
    with TestClient(app) as c:
        _onboarded_seat(c)
        transport = TestClient(app)
        transport.headers.update(H1)  # header identity, no bearer token
        client = AthenaClient(client=transport)
        try:
            _seat_doctor_checks(client, expect_scopes=None)
            raise AssertionError("expected header auth to fail")
        except ValueError as exc:
            assert "bearer token" in str(exc)


def test_dead_token_surfaces_the_auth_error(tmp_path):
    """A revoked or mistyped token fails at whoami with the transport error —
    after healthz has already passed, so the failure is attributable to the
    credential, not to an unreachable server."""
    app = _app(tmp_path)
    with TestClient(app) as c:
        _onboarded_seat(c)
        client = _bearer_client(app, "ath_definitely_not_a_live_token")
        try:
            _seat_doctor_checks(client, expect_scopes=None)
            raise AssertionError("expected the dead token to fail")
        except AthenaError as exc:
            assert exc.status_code == 401


def test_main_refuses_to_run_unwired(monkeypatch, capsys):
    """No URL or no token is itself the diagnosis: exit 1 with the fix named,
    never a stack trace and never a green exit."""
    monkeypatch.delenv("ATHENA_BASE_URL", raising=False)
    monkeypatch.delenv("ATHENA_TOKEN", raising=False)
    assert seat_doctor_main([]) == 1
    err = capsys.readouterr().err
    assert "ATHENA_BASE_URL" in err

    assert seat_doctor_main(["--base-url", "http://127.0.0.1:9"]) == 1
    err = capsys.readouterr().err
    assert "ATHENA_TOKEN" in err


def test_main_runs_the_full_walk_and_maps_errors(tmp_path, monkeypatch, capsys):
    """The entrypoint end to end: exit 0 with the five ok lines on a wired
    seat, exit 1 with the transport diagnosis on a dead token — proving the
    error mapping the operator will actually see, not just the checks."""
    import athena.mcp.client as mcp_client

    app = _app(tmp_path)
    with TestClient(app) as c:
        seat = _onboarded_seat(c)
        raw = seat["token"]["token"]

        def _client_factory(base_url, token, timeout):
            transport = TestClient(app)
            transport.headers.update({"Authorization": f"Bearer {token}"})
            return AthenaClient(client=transport)

        # seat_doctor_main imports AthenaClient lazily from the client module
        # (the base install lacks httpx), so THAT module is the patch point.
        monkeypatch.setattr(mcp_client, "AthenaClient", _client_factory)

        assert (
            seat_doctor_main(
                [
                    "--base-url",
                    "http://test",
                    "--token",
                    raw,
                    "--expect-scopes",
                    "read,issue:write",
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "athena-seat-doctor: ok" in out
        assert out.count(": ok") >= 5

        assert (
            seat_doctor_main(["--base-url", "http://test", "--token", "ath_dead"]) == 1
        )
        err = capsys.readouterr().err
        assert "FAIL" in err and "401" in err

        assert (
            seat_doctor_main(
                ["--base-url", "http://test", "--token", raw, "--expect-scopes", " , "]
            )
            == 1
        )
        assert "empty" in capsys.readouterr().err


def test_ops_imports_without_httpx_reaching_sys_modules():
    """The base (server-only) install has no httpx — it lives in the [mcp]
    extra. athena-serve therefore must be able to import athena.ops without
    httpx ever loading; a module-level import of the MCP client here broke
    the container boot once already. This pins the lazy-import contract in
    the environment that HAS httpx, by proving the import graph alone never
    pulls it in."""
    import subprocess
    import sys

    probe = (
        "import sys; import athena.ops; "
        "assert 'httpx' not in sys.modules, 'athena.ops pulled in httpx'"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
