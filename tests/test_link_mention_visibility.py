"""Link-mention treats a hidden target exactly as a missing one (SEC-2, C3).

Visibility is frozen: a page in a private space or an issue in a private project
reads as MISSING to someone outside it — never 403, never a different 404. The
review found link-mention resolving its target with no actor at all. For a
non-member, naming a hidden target answered 409 ("that mention is no longer in
this page") where a missing id answered 404, and with the guessed title or key
in the body it answered 200 and wrote the link. Any writer could enumerate which
private ids exist and confirm a guessed title or key.

The test is differential on purpose. It does not pin a status; it pins that the
whole response — status and body bytes — for a hidden target is
indistinguishable from the one for an id that does not exist, on both REST
routes and both browser twins, for both target kinds, whether or not the
source's text happens to contain the guessed name.
"""

from fastapi.testclient import TestClient

from athena.core import access, db
from athena.main import create_app

H_OP = {"X-Athena-Actor": "1"}
H_BOB = {"X-Athena-Actor": "2"}
H_CAROL = {"X-Athena-Actor": "3"}
MISSING = 987654
GUESS = "see the axolotl vault page and VLP-1 before release"
PLAIN = "nothing that names anything private"


def _user(client, email, name):
    r = client.post(
        "/users",
        json={"email": email, "name": name, "password": "pw", "role": "member"},
        headers=H_OP,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _world(client, db_file):
    """The operator's private space and project, Bob outside both, Carol inside."""
    client.post("/users", json={"email": "op@e.com", "name": "Op", "password": "pw"})
    bob = _user(client, "bob@e.com", "Bob")
    carol = _user(client, "carol@e.com", "Carol")

    vault = client.post(
        "/spaces", json={"key": "VLT", "name": "Vault"}, headers=H_OP
    ).json()["id"]
    hidden_page = client.post(
        f"/spaces/{vault}/pages", json={"title": "axolotl vault page"}, headers=H_OP
    ).json()["id"]
    vlp = client.post(
        "/projects", json={"name": "Vault plans", "key": "VLP"}, headers=H_OP
    ).json()["id"]
    hidden_issue = client.post(
        "/issues", json={"title": "launch", "project_id": vlp}, headers=H_OP
    ).json()
    assert hidden_issue["key"] == "VLP-1"

    conn = db.connect(db_file)
    try:
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (vault,))
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (vlp,))
        conn.commit()
        access.add_space_member(conn, vault, carol, added_by=1)
        access.add_project_member(conn, vlp, carol, added_by=1)
    finally:
        conn.close()

    # Bob can't see either; it reads as missing everywhere else already.
    assert client.get(f"/pages/{hidden_page}", headers=H_BOB).status_code == 404
    assert client.get(f"/issues/{hidden_issue['id']}", headers=H_BOB).status_code == 404

    public = client.post(
        "/spaces", json={"key": "PUB", "name": "Public"}, headers=H_BOB
    ).json()["id"]
    bobs = client.post(
        "/projects", json={"name": "Bob work", "key": "BOB"}, headers=H_BOB
    ).json()["id"]
    return {
        "bob": bob,
        "carol": carol,
        "public": public,
        "bobs_project": bobs,
        "hidden_page": hidden_page,
        "hidden_issue": hidden_issue["id"],
    }


def _sources(client, w, headers, body):
    page = client.post(
        f"/spaces/{w['public']}/pages",
        json={"title": f"notes {len(body)} {headers['X-Athena-Actor']}", "body": body},
        headers=headers,
    ).json()["id"]
    issue = client.post(
        "/issues",
        json={"title": "notes", "project_id": w["bobs_project"], "body": body},
        headers=headers,
    ).json()["id"]
    return {"page": page, "issue": issue}


def _targets(w):
    return [
        ("page", w["hidden_page"]),
        ("page", MISSING),
        ("issue", w["hidden_issue"]),
        ("issue", MISSING),
    ]


def _trail(db_file):
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    finally:
        conn.close()


def test_rest_link_mention_cannot_tell_hidden_from_missing(tmp_path):
    db_file = tmp_path / "rest.db"
    with TestClient(create_app(db_file)) as c:
        w = _world(c, db_file)
        for body in (PLAIN, GUESS):
            sources = _sources(c, w, H_BOB, body)
            before = _trail(db_file)
            for source_kind, base in (("page", "/pages"), ("issue", "/issues")):
                source_id = sources[source_kind]
                answers = {}
                for target_kind, target_id in _targets(w):
                    r = c.post(
                        f"{base}/{source_id}/link-mention",
                        json={"target_kind": target_kind, "target_id": target_id},
                        headers=H_BOB,
                    )
                    answers[(target_kind, target_id)] = (r.status_code, r.content)
                # One answer for all four: hidden page, missing page, hidden issue,
                # missing issue — on this route, with this body.
                assert len(set(answers.values())) == 1, (source_kind, body, answers)
                assert next(iter(answers.values()))[0] == 404
                # And nothing was written: no link, no rewritten body, no event.
                stored = c.get(f"{base}/{source_id}", headers=H_BOB).json()
                assert stored["body"] == body
            assert _trail(db_file) == before


def test_browser_link_mention_cannot_tell_hidden_from_missing(tmp_path):
    db_file = tmp_path / "web.db"
    with TestClient(create_app(db_file)) as c:
        w = _world(c, db_file)
        plain = _sources(c, w, H_BOB, PLAIN)
        guess = _sources(c, w, H_BOB, GUESS)
        assert (
            c.post(
                "/login",
                data={"email": "bob@e.com", "password": "pw"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        before = _trail(db_file)
        for sources, body in ((plain, PLAIN), (guess, GUESS)):
            for source_kind, path in (
                ("page", "/mentor/pages/{}/link-mention"),
                ("issue", "/aegis/issues/{}/link-mention"),
            ):
                answers = {}
                for target_kind, target_id in _targets(w):
                    r = c.post(
                        path.format(sources[source_kind]),
                        data={"target_kind": target_kind, "target_id": target_id},
                        follow_redirects=False,
                    )
                    answers[(target_kind, target_id)] = (r.status_code, r.content)
                assert len(set(answers.values())) == 1, (source_kind, body, answers)
                assert next(iter(answers.values()))[0] == 404
        assert _trail(db_file) == before
        for kind, base in (("page", "/pages"), ("issue", "/issues")):
            assert c.get(f"{base}/{guess[kind]}", headers=H_BOB).json()["body"] == GUESS


def test_a_member_with_access_still_links_the_same_targets(tmp_path):
    # The positive control: the refusal above is about who is asking, not about
    # the targets. Carol is a member of the private space and project, so for
    # her the exact same request links — on both routes, for both kinds.
    db_file = tmp_path / "member.db"
    with TestClient(create_app(db_file)) as c:
        w = _world(c, db_file)
        for target_kind, target_id, token in (
            ("page", w["hidden_page"], "[[axolotl vault page]]"),
            ("issue", w["hidden_issue"], "[[VLP-1]]"),
        ):
            sources = _sources(c, w, H_CAROL, GUESS)
            for base in ("/pages", "/issues"):
                source_id = sources[base.strip("/").rstrip("s")]
                r = c.post(
                    f"{base}/{source_id}/link-mention",
                    json={"target_kind": target_kind, "target_id": target_id},
                    headers=H_CAROL,
                )
                assert r.status_code == 200, (base, target_kind, r.text)
                assert token in r.json()["body"]
        # Bob linking a target he CAN see is unaffected too.
        bob_target = c.post(
            f"/spaces/{w['public']}/pages",
            json={"title": "open runbook"},
            headers=H_BOB,
        ).json()["id"]
        source = _sources(c, w, H_BOB, "read the open runbook first")["page"]
        r = c.post(
            f"/pages/{source}/link-mention",
            json={"target_kind": "page", "target_id": bob_target},
            headers=H_BOB,
        )
        assert r.status_code == 200, r.text
        assert "[[open runbook]]" in r.json()["body"]
