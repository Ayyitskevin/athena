"""The Label hub — browse everything tagged with a label, across issues and pages.

Labels now live in core, so one label can sit on an issue AND a page; the hub is the
payoff — a cross-entity view that gathers both. These pin the index (labels with
issue/page counts), the per-label detail (the tagged issues + pages, case-insensitive
name, archived issues hidden), the 404, and that label chips link into it.
"""

from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A"})


def _issue(client, title="x"):
    return client.post("/issues", json={"title": title}, headers=H1).json()["id"]


def _label(client, name="urgent", color="#ff0000"):
    return client.post(
        "/labels", json={"name": name, "color": color}, headers=H1
    ).json()["id"]


def _space(client):
    return client.post(
        "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
    ).json()["id"]


def _page(client, sid, title="Doc"):
    return client.post(
        f"/spaces/{sid}/pages", json={"title": title}, headers=H1
    ).json()["id"]


def _tag_issue(client, iid, lid):
    client.post(f"/issues/{iid}/labels", json={"label_id": lid}, headers=H1)


def _tag_page(client, pid, lid):
    client.post(f"/pages/{pid}/labels", json={"label_id": lid}, headers=H1)


def test_index_shows_labels_with_counts(tmp_path):
    with TestClient(create_app(tmp_path / "idx.db")) as client:
        _bootstrap(client)
        lid = _label(client)
        _tag_issue(client, _issue(client, "one"), lid)
        _tag_issue(client, _issue(client, "two"), lid)
        _tag_page(client, _page(client, _space(client)), lid)

        page = client.get("/aegis/labels")
        assert page.status_code == 200
        text = page.text
        assert "urgent" in text
        # 2 issues + 1 page — the counts the index renders.
        assert ">2<" in text and ">1<" in text


def test_detail_gathers_issues_and_pages(tmp_path):
    with TestClient(create_app(tmp_path / "det.db")) as client:
        _bootstrap(client)
        lid = _label(client)
        iid = _issue(client, "Fix login")
        _tag_issue(client, iid, lid)
        pid = _page(client, _space(client), "Incident runbook")
        _tag_page(client, pid, lid)
        # An issue WITHOUT the label must not appear.
        _issue(client, "Unrelated")

        text = client.get("/aegis/labels/urgent").text
        assert "Fix login" in text and "Incident runbook" in text
        assert "Unrelated" not in text


def test_detail_is_case_insensitive(tmp_path):
    with TestClient(create_app(tmp_path / "case.db")) as client:
        _bootstrap(client)
        lid = _label(client, "Frontend")
        _tag_issue(client, _issue(client, "Tweak CSS"), lid)
        # The label is stored "Frontend"; the URL cased differently still resolves.
        assert "Tweak CSS" in client.get("/aegis/labels/FRONTEND").text


def test_archived_issue_is_hidden_from_the_hub(tmp_path):
    with TestClient(create_app(tmp_path / "arch.db")) as client:
        _bootstrap(client)
        lid = _label(client)
        gone = _issue(client, "Old bug")
        _tag_issue(client, gone, lid)
        client.post(f"/issues/{gone}/archive", headers=H1)
        # Archived issues drop out of every list, the hub included.
        assert "Old bug" not in client.get("/aegis/labels/urgent").text


def test_unknown_label_is_404(tmp_path):
    with TestClient(create_app(tmp_path / "missing.db")) as client:
        _bootstrap(client)
        r = client.get("/aegis/labels/nope")
        assert r.status_code == 404 and "not found" in r.text.lower()


def test_chips_link_into_the_hub(tmp_path):
    with TestClient(create_app(tmp_path / "chips.db")) as client:
        _bootstrap(client)
        lid = _label(client)
        iid = _issue(client, "Tagged")
        _tag_issue(client, iid, lid)
        pid = _page(client, _space(client))
        _tag_page(client, pid, lid)

        # The chip on the issue detail, the issue list, and the page detail all link
        # to the label's hub view.
        assert "/aegis/labels/urgent" in client.get(f"/aegis/issues/{iid}").text
        assert "/aegis/labels/urgent" in client.get("/aegis/issues").text
        assert "/aegis/labels/urgent" in client.get(f"/mentor/pages/{pid}").text
