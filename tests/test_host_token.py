"""The first token can be minted on the host (H17, OPS-9).

Before: the only way to mint a token was the browser form, so an instance with
no browser at hand, or an agent's account, could not be given its first
credential from the machine it runs on. ``athena-token`` mints one for a named
account through the audited command and prints it once.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, bearer, bootstrap, make_app, onboard_agent

from athena import ops
from athena.core import db


def _events(db_file) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT actor_id, target_id, detail FROM activity "
                "WHERE verb = 'minted_token' ORDER BY id"
            )
        ]
    finally:
        conn.close()


def test_the_command_prints_a_working_token_once_and_the_trail_says_how(
    tmp_path, capsys
):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        before = len(_events(db_file))
        code = ops.token_main(
            [str(db_file), "a@e.com", "--name", "first", "--scope", "read"]
        )
        out, err = capsys.readouterr()
        raw = out.strip()
        me = client.get("/users/me", headers=bearer(raw))
        refused = client.post("/issues", json={"title": "x"}, headers=bearer(raw))

    assert code == 0
    # The secret alone on stdout; what it is, on stderr, without the secret.
    assert raw.startswith("ath_") and "\n" not in raw
    assert raw not in err and "a@e.com" in err and "read" in err
    assert me.status_code == 200 and me.json()["email"] == "a@e.com"
    assert me.json()["scopes"] == ["read"]
    # The scope asked for and no more.
    assert refused.status_code == 403
    (event,) = _events(db_file)[before:]
    assert event["actor_id"] == 1
    assert event["detail"] == "first [read]; via athena-token on the host"
    assert raw not in event["detail"]


def test_several_scopes_for_an_agents_account(tmp_path, capsys):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        onboard_agent(client, email="sol@e.com", name="Sol")
        code = ops.token_main(
            [
                str(db_file),
                "sol@e.com",
                "--name",
                "seat",
                "--scope",
                "read",
                "--scope",
                "issue:write",
            ]
        )
        raw = capsys.readouterr().out.strip()
        me = client.get("/users/me", headers=bearer(raw))

    assert code == 0
    assert me.json()["email"] == "sol@e.com"
    assert sorted(me.json()["scopes"]) == ["issue:write", "read"]


def test_an_account_that_cannot_use_a_token_is_refused(tmp_path, capsys):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        paused = client.put(
            f"/users/{sol['user']['id']}/paused", json={"paused": True}, headers=ADMIN
        )
        assert paused.status_code == 200, paused.text
        gone = onboard_agent(client, email="gone@e.com", name="Gone")
        removed = client.post(f"/users/{gone['user']['id']}/remove", headers=ADMIN)
        assert removed.status_code in (200, 204), removed.text
        before = len(_events(db_file))
        codes = {
            email: ops.token_main(
                [str(db_file), email, "--name", "n", "--scope", "read"]
            )
            for email in ("nobody@e.com", "sol@e.com", "gone@e.com")
        }
        out, err = capsys.readouterr()

    assert codes == {"nobody@e.com": 1, "sol@e.com": 1, "gone@e.com": 1}
    assert out == ""
    assert "no account has the email nobody@e.com" in err
    assert "sol@e.com is paused" in err
    # Removal takes the account's email away, so there is nothing to name.
    assert "no account has the email gone@e.com" in err
    assert len(_events(db_file)) == before


def test_a_scope_must_be_named_and_known(tmp_path, capsys):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
    for arguments in (
        [str(db_file), "a@e.com", "--name", "n"],
        [str(db_file), "a@e.com", "--name", "n", "--scope", "everything"],
    ):
        with pytest.raises(SystemExit) as stopped:
            ops.token_main(arguments)
        assert stopped.value.code == 2
    assert capsys.readouterr().out == ""


def test_a_database_that_is_not_athenas_is_refused(tmp_path, capsys):
    missing = tmp_path / "nowhere.db"
    code = ops.token_main([str(missing), "a@e.com", "--name", "n", "--scope", "read"])
    out, err = capsys.readouterr()

    assert code == 1 and out == ""
    assert err.startswith("athena-token: ")
    # Asking for a token never creates a database.
    assert not missing.exists()
