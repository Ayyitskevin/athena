"""GET /activity/export.csv — the audit trail as a CSV download.

The REST twin of the web Activity page's CSV export, for compliance/archival
tooling. It shares the feed's authenticated gate and filter set over the same
data-access read, so the two can never disagree on what matches — only the
representation (CSV) and the higher row cap differ. These pin the CSV shape, the
filters, the target_id/limit guards, and the auth requirement.
"""
import csv
from io import StringIO

from fastapi.testclient import TestClient

from athena import config
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}

_HEADER = ["id", "created_at", "actor_id", "actor_name", "verb", "target_kind", "target_id", "detail"]


def _rows(text):
    return list(csv.DictReader(StringIO(text)))


def _seed(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann"})
    issue = client.post("/issues", json={"title": "Audit me"}, headers=H1).json()
    client.patch(f"/issues/{issue['id']}", json={"status": "in_progress"}, headers=H1)
    return issue["id"]


def test_csv_shape_and_headers(tmp_path):
    with TestClient(create_app(tmp_path / "shape.db")) as client:
        _seed(client)
        r = client.get("/activity/export.csv", headers=H1)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"] == 'attachment; filename="athena-activity.csv"'
        rows = _rows(r.text)
        # Stable operator columns, and both lifecycle facts present (newest first).
        assert r.text.splitlines()[0].split(",") == _HEADER
        verbs = [row["verb"] for row in rows]
        assert verbs == ["changed_status", "created"]
        assert rows[0]["detail"] == "open → in_progress" and rows[0]["actor_name"] == "Ann"


def test_filters_match_the_feed(tmp_path):
    with TestClient(create_app(tmp_path / "filter.db")) as client:
        _seed(client)
        # verb filter narrows exactly like the JSON feed.
        only_created = _rows(client.get("/activity/export.csv?verb=created", headers=H1).text)
        assert [r["verb"] for r in only_created] == ["created"]
        # A non-matching actor yields just the header, no rows.
        none = client.get("/activity/export.csv?actor_id=999", headers=H1).text
        assert _rows(none) == [] and none.splitlines()[0].split(",") == _HEADER


def test_target_id_requires_kind(tmp_path):
    with TestClient(create_app(tmp_path / "tgt.db")) as client:
        _seed(client)
        assert client.get("/activity/export.csv?target_id=1", headers=H1).status_code == 422
        # With the kind, it's a valid one-target export.
        ok = client.get("/activity/export.csv?target_id=1&target_kind=issue", headers=H1)
        assert ok.status_code == 200 and len(_rows(ok.text)) == 2


def test_limit_caps_and_pages(tmp_path):
    with TestClient(create_app(tmp_path / "limit.db")) as client:
        _seed(client)
        # Over the cap is rejected, not silently clamped.
        assert client.get("/activity/export.csv?limit=99999", headers=H1).status_code == 422
        # limit=1 returns the newest row only; before_id pages to the next.
        first = _rows(client.get("/activity/export.csv?limit=1", headers=H1).text)
        assert len(first) == 1
        nxt = _rows(
            client.get(f"/activity/export.csv?limit=1&before_id={first[0]['id']}", headers=H1).text
        )
        assert len(nxt) == 1 and int(nxt[0]["id"]) < int(first[0]["id"])


def test_export_requires_auth(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "auth.db")) as client:
        _seed(client)
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        assert client.get("/activity/export.csv").status_code == 401
